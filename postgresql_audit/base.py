import os
import string
import warnings
from contextlib import contextmanager
from datetime import timedelta
from weakref import WeakSet

import sqlalchemy as sa
from sqlalchemy import orm
from sqlalchemy.dialects.postgresql import array, INET, JSONB
from sqlalchemy.dialects.postgresql.base import PGDialect
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy_utils import get_class_by_table, get_primary_keys

from .expressions import ExpressionReflector, jsonb_merge

HERE = os.path.dirname(os.path.abspath(__file__))
cached_statements = {}


class ImproperlyConfigured(Exception):
    pass


class StatementExecutor(object):
    def __init__(self, stmt):
        self.stmt = stmt

    def __call__(self, target, bind, **kwargs):
        tx = bind.begin()
        bind.execute(self.stmt)
        tx.commit()


def read_file(file_):
    with open(os.path.join(HERE, file_)) as f:
        s = f.read()
    return s


def assign_actor(base, cls, actor_cls):
    if hasattr(cls, 'actor_id'):
        return
    if actor_cls:
        primary_key = sa.inspect(actor_cls).primary_key[0]

        cls.actor_id = sa.Column('actor_id', primary_key.type)
        cls.actor = orm.relationship(
            actor_cls,
            primaryjoin=cls.actor_id == (
                getattr(
                    actor_cls,
                    primary_key.name
                )
            ),
            foreign_keys=[cls.actor_id]
        )
    else:
        cls.actor_id = sa.Column(sa.Text)


def activity_base(base, schema=None):
    class ActivityBase(base):
        __abstract__ = True
        __table_args__ = {'schema': schema}
        id = sa.Column(sa.BigInteger, primary_key=True)
        schema_name = sa.Column(sa.Text)
        table_name = sa.Column(sa.Text)
        relid = sa.Column(sa.Integer)
        issued_at = sa.Column(sa.DateTime)
        transaction_id = sa.Column(sa.BigInteger)
        client_addr = sa.Column(INET)
        verb = sa.Column(sa.Text)
        target_id = sa.Column(sa.Text)
        old_data = sa.Column(JSONB)
        changed_data = sa.Column(JSONB)

        @hybrid_property
        def data(self):
            data = self.old_data.copy() if self.old_data else {}
            if self.changed_data:
                data.update(self.changed_data)
            return data

        @data.expression
        def data(cls):
            return jsonb_merge(cls.old_data, cls.changed_data)

        @property
        def object(self):
            table = base.metadata.tables[self.table_name]
            cls = get_class_by_table(base, table, self.data)
            return cls(**self.data)

        def __repr__(self):
            return (
                '<{cls} table_name={table_name!r} '
                'id={id!r}>'
            ).format(
                cls=self.__class__.__name__,
                table_name=self.table_name,
                id=self.id
            )
    return ActivityBase


def convert_callables(values):
    return {
        key: value() if callable(value) else value
        for key, value in values.items()
    }


class VersioningManager(object):
    _actor_cls = None

    def __init__(self, actor_cls=None, schema_name=None):
        if actor_cls is not None:
            self._actor_cls = actor_cls
        self.values = {}
        self.listeners = (
            (
                orm.mapper,
                'instrument_class',
                self.instrument_versioned_classes
            ),
            (
                orm.mapper,
                'after_configured',
                self.configure_versioned_classes
            ),
            (
                orm.session.Session,
                'after_flush',
                self.receive_after_flush,
            ),
        )
        self.schema_name = schema_name
        self.table_listener_mapping = self.get_table_listener_mapping()
        self.table_listeners = list(self.table_listener_mapping.items())
        self.pending_classes = WeakSet()
        self.cached_ddls = {}

    def get_transaction_values(self):
        return self.values

    @contextmanager
    def disable(self, session):
        current_setting = session.execute(
            "SELECT current_setting('session_replication_role')"
        ).fetchone().current_setting
        session.execute('SET LOCAL session_replication_role = "local"')
        yield
        session.execute('SET LOCAL session_replication_role = "{}"'.format(
            current_setting,
        ))

    def render_tmpl(self, tmpl_name):
        file_contents = read_file(
            'templates/{}'.format(tmpl_name)
        ).replace('%', '%%')
        tmpl = string.Template(file_contents)
        context = dict(schema_name=self.schema_name)

        if self.schema_name is None:
            context['schema_prefix'] = ''
            context['revoke_cmd'] = ''
        else:
            context['schema_prefix'] = '{}.'.format(self.schema_name)
            context['revoke_cmd'] = (
                'REVOKE ALL ON {schema_prefix}activity FROM public;'
            ).format(**context)

        return tmpl.substitute(**context)

    def get_table_listener_mapping(self):
        mapping = {
            'after_create': sa.schema.DDL(
                self.render_tmpl('create_activity.sql') +
                self.render_tmpl('audit_table_func.sql')
            ),
        }
        if self.schema_name is not None:
            mapping.update({
                'before_create': sa.schema.DDL(
                    self.render_tmpl('create_schema.sql')
                ),
                'after_drop': sa.schema.DDL(
                    self.render_tmpl('drop_schema.sql')
                ),
            })
        return mapping

    def audit_table(self, table, exclude_columns=None):
        args = [table.name]
        if exclude_columns:
            for column in exclude_columns:
                if column not in table.c:
                    raise ImproperlyConfigured(
                        "Could not configure versioning. Table '{}'' does "
                        "not have a column named '{}'.".format(
                            table.name, column
                        )
                    )
            args.append(array(exclude_columns))

        if self.schema_name is None:
            func = sa.func.audit_table
        else:
            func = getattr(getattr(sa.func, self.schema_name), 'audit_table')
        query = sa.select([func(*args)])
        if query not in cached_statements:
            cached_statements[query] = StatementExecutor(query)
        listener = (table, 'after_create', cached_statements[query])
        if not sa.event.contains(*listener):
            sa.event.listen(*listener)

    def set_activity_values(self, session):
        dialect = session.bind.engine.dialect
        table = self.activity_cls.__table__

        if not isinstance(dialect, PGDialect):
            warnings.warn(
                '"{0}" is not a PostgreSQL dialect. No versioning data will '
                'be saved.'.format(dialect.__class__),
                RuntimeWarning
            )
            return

        values = convert_callables(self.get_transaction_values())

        if values:
            stmt = (
                table
                .update()
                .values(**values)
                .where(
                    sa.and_(
                        table.c.transaction_id == sa.func.txid_current(),
                        table.c.issued_at > (
                            sa.func.now() - timedelta(days=1)
                        )
                    )
                )
            )
            session.execute(stmt)

    def receive_after_flush(self, session, flush_context):
        self.set_activity_values(session)

    def instrument_versioned_classes(self, mapper, cls):
        """
        Collect versioned class and add it to pending_classes list.

        :mapper mapper: SQLAlchemy mapper object
        :cls cls: SQLAlchemy declarative class
        """
        if hasattr(cls, '__versioned__') and cls not in self.pending_classes:
            self.pending_classes.add(cls)

    def configure_versioned_classes(self):
        """
        Configures all versioned classes that were collected during
        instrumentation process.
        """
        for cls in self.pending_classes:
            self.audit_table(cls.__table__, cls.__versioned__.get('exclude'))
        assign_actor(self.base, self.activity_cls, self.actor_cls)

    def attach_table_listeners(self):
        for values in self.table_listeners:
            sa.event.listen(self.activity_cls.__table__, *values)

    def remove_table_listeners(self):
        for values in self.table_listeners:
            sa.event.remove(self.activity_cls.__table__, *values)

    @property
    def actor_cls(self):
        if isinstance(self._actor_cls, str):
            if not self.base:
                raise ImproperlyConfigured(
                    'This manager does not have declarative base set up yet. '
                    'Call init method to set up this manager.'
                )
            registry = self.base._decl_class_registry
            try:
                return registry[self._actor_cls]
            except KeyError:
                raise ImproperlyConfigured(
                    'Could not build relationship between Activity'
                    ' and %s. %s was not found in declarative class '
                    'registry. Either configure VersioningManager to '
                    'use different actor class or disable this '
                    'relationship by setting it to None.' % (
                        self._actor_cls,
                        self._actor_cls
                    )
                )
        return self._actor_cls

    def attach_listeners(self):
        self.attach_table_listeners()
        for listener in self.listeners:
            sa.event.listen(*listener)

    def remove_listeners(self):
        self.remove_table_listeners()
        for listener in self.listeners:
            sa.event.remove(*listener)

    def activity_model_factory(self, base):
        class Activity(activity_base(base, self.schema_name)):
            __tablename__ = 'activity'

        return Activity

    def init(self, base):
        self.base = base
        self.activity_cls = self.activity_model_factory(base)
        self.attach_listeners()

    def build_condition_for_obj(self, obj):
        return sa.and_(
            self.activity_cls.table_name == obj.__tablename__,
            *(
                self.activity_cls.data[c.name].astext ==
                str(getattr(obj, c.name))
                for c in get_primary_keys(obj).values()
            )
        )

    def get_last_transaction_id_query(self, obj, time=None):
        condition = self.build_condition_for_obj(obj)
        if time:
            condition = sa.and_(condition, self.activity_cls.issued_at < time)
        return sa.select(
            [sa.func.max(self.activity_cls.transaction_id)]
        ).where(condition)

    def resurrect(self, session, model, expr):
        """

        Resurrects objects for given session and given expression.

        ::


            versioning_manager.resurrect(
                session,
                User,
                User.id == 3
            )

        This method uses the greatest-n-per-group algorithm, for more info
        see: http://stackoverflow.com/questions/7745609
        """
        reflected = ExpressionReflector(self.activity_cls)(expr)
        alias = sa.orm.aliased(self.activity_cls)
        query = sa.select([self.activity_cls.data]).select_from(
            self.activity_cls.__table__.outerjoin(
                alias,
                sa.and_(
                    self.activity_cls.table_name == alias.table_name,
                    sa.and_(
                        self.activity_cls.data[c.name] == alias.data[c.name]
                        for c in get_primary_keys(model).values()
                    ),
                    self.activity_cls.issued_at < alias.issued_at
                )
            )
        ).where(
            sa.and_(
                alias.id.is_(None),
                reflected,
                self.activity_cls.verb == 'delete'
            )
        )
        data = session.execute(query).fetchall()
        for row in data:
            session.add(model(**row[0]))

    def revert(self, obj, time):
        """
        Revert an object's data to given point in time.

        ::


            versioning_manager.revert(user, datetime(2011, 1, 1))
        """

        Activity = self.activity_cls
        query = sa.select(
            [Activity.data]
        ).where(
            sa.and_(
                Activity.transaction_id == self.get_last_transaction_id_query(
                    obj,
                    time
                ),
                self.build_condition_for_obj(obj)
            )
        )
        session = sa.orm.object_session(obj)

        data = session.execute(query).scalar()
        for key, value in data.items():
            setattr(obj, key, value)


versioning_manager = VersioningManager()

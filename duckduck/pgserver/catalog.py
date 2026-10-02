"""
What a PostgreSQL client sees of duckduck's tables: a ``pg_catalog`` of its
own, in the DuckDB schema ``duckduck_pg`` (DuckDB's built-in one can't be
replaced and gives columns the wrong types), plus ``information_schema``
views in ``duckduck_is``.

- **Schemas** are connectors, and connector + database: ``nvd`` holds
  ``cves`` (``SELECT * FROM nvd.cves``), ``s3_data.security`` holds the saved
  ``s3_data.security.proxy_logs``. Tables without an address go in ``public``.
- **Tables** are every table that needs no arguments — plain tables,
  catalogs, saved tables (a saved query shows as a view, with its SQL), taken
  over answers. A table function needs its arguments, so it isn't a table:
  query it by address (``SELECT * FROM glue.security.proxy_logs``), or save it.
- **Columns** come from what duckduck has read: an API's columns are only
  known once a query read the table. Until then the table shows one column,
  ``run_a_query_to_list_columns``. What's learned is kept in ``columns_file``.

A client's catalog query is rewritten (``rewrite``) to read these tables:
``pg_catalog.x`` → ``duckduck_pg.x``, ``'pg_class'::regclass`` → its oid,
``::name`` → ``::VARCHAR``, PostgreSQL functions DuckDB lacks → macros.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import zlib
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import protocol as pg

logger = logging.getLogger("duckduck.pgserver")

SCHEMA = "duckduck_pg"
INFO_SCHEMA = "duckduck_is"
UNKNOWN_COLUMN = "run_a_query_to_list_columns"
SERVER_VERSION = "14.0"

_OWNER = 10
_PROC_BASE = 60000  # the types' I/O functions (pg_proc)
_NS_CATALOG, _NS_PUBLIC, _NS_INFO = 11, 2200, 13000

#: the system catalogs' own oids (for ``'pg_class'::regclass``)
_SYSTEM_OIDS = {"pg_type": 1247, "pg_attribute": 1249, "pg_proc": 1255, "pg_class": 1259, "pg_database": 1262,
                "pg_namespace": 2615, "pg_constraint": 2606, "pg_index": 2610, "pg_description": 2609,
                "pg_trigger": 2620, "pg_rewrite": 2618, "pg_attrdef": 2604, "pg_am": 2601, "pg_depend": 2608,
                "pg_inherits": 2611, "pg_tablespace": 1213, "pg_roles": 1260, "pg_authid": 1260,
                "pg_extension": 3079, "pg_settings": 12000, "pg_collation": 3456, "pg_language": 2612,
                "pg_operator": 2617, "pg_opclass": 2616, "pg_cast": 2605, "pg_enum": 3501,
                "pg_foreign_table": 3118, "pg_foreign_server": 1417, "pg_foreign_data_wrapper": 2328,
                "pg_event_trigger": 3466, "pg_policy": 3256, "pg_sequence": 2224, "pg_statistic_ext": 3381,
                "pg_partitioned_table": 3350, "pg_publication": 6104, "pg_subscription": 6100,
                "pg_shdescription": 2396, "pg_auth_members": 1261, "pg_user": 12001, "pg_views": 12002,
                "pg_tables": 12003, "pg_matviews": 12004, "pg_stat_activity": 12005, "pg_locks": 12006,
                "pg_aggregate": 2600, "pg_conversion": 2607, "pg_range": 3541, "pg_init_privs": 3394,
                "pg_seclabel": 3596, "pg_ts_config": 3602, "pg_default_acl": 826, "pg_db_role_setting": 2964,
                "pg_largeobject_metadata": 2995, "pg_user_mapping": 1418, "pg_transform": 3576,
                "pg_statistic": 2619, "pg_stat_user_tables": 12007, "pg_indexes": 12008}

# name → (columns). Types: DuckDB's; oid columns are UINTEGER (they go out as PostgreSQL's oid).
_O, _T, _B, _I2, _I4, _I8, _F4 = "UINTEGER", "VARCHAR", "BOOLEAN", "SMALLINT", "INTEGER", "BIGINT", "FLOAT"
TABLES: Dict[str, List[Tuple[str, str]]] = {
    "pg_namespace": [("oid", _O), ("nspname", _T), ("nspowner", _O), ("nspacl", "VARCHAR[]")],
    "pg_class": [("oid", _O), ("relname", _T), ("relnamespace", _O), ("reltype", _O), ("reloftype", _O),
                 ("relowner", _O), ("relam", _O), ("relfilenode", _O), ("reltablespace", _O), ("relpages", _I4),
                 ("reltuples", _F4), ("relallvisible", _I4), ("reltoastrelid", _O), ("relhasindex", _B),
                 ("relisshared", _B), ("relpersistence", _T), ("relkind", _T), ("relnatts", _I2), ("relchecks", _I2),
                 ("relhasoids", _B), ("relhasrules", _B), ("relhastriggers", _B), ("relhassubclass", _B),
                 ("relrowsecurity", _B), ("relforcerowsecurity", _B), ("relispopulated", _B), ("relreplident", _T),
                 ("relispartition", _B), ("relrewrite", _O), ("relfrozenxid", _O), ("relminmxid", _O),
                 ("relacl", "VARCHAR[]"), ("reloptions", "VARCHAR[]"), ("relpartbound", _T)],
    "pg_attribute": [("attrelid", _O), ("attname", _T), ("atttypid", _O), ("attstattarget", _I4), ("attlen", _I2),
                     ("attnum", _I2), ("attndims", _I4), ("attcacheoff", _I4), ("atttypmod", _I4), ("attbyval", _B),
                     ("attstorage", _T), ("attalign", _T), ("attnotnull", _B), ("atthasdef", _B),
                     ("atthasmissing", _B), ("attidentity", _T), ("attgenerated", _T), ("attisdropped", _B),
                     ("attcompression", _T),
                     ("attislocal", _B), ("attinhcount", _I4), ("attcollation", _O), ("attacl", "VARCHAR[]"),
                     ("attoptions", "VARCHAR[]"), ("attfdwoptions", "VARCHAR[]"), ("attmissingval", _T)],
    "pg_type": [("oid", _O), ("typname", _T), ("typnamespace", _O), ("typowner", _O), ("typlen", _I2),
                ("typbyval", _B), ("typtype", _T), ("typcategory", _T), ("typispreferred", _B),
                ("typisdefined", _B), ("typdelim", _T), ("typrelid", _O), ("typsubscript", _T), ("typelem", _O),
                ("typarray", _O), ("typinput", _O), ("typoutput", _O), ("typreceive", _O), ("typsend", _O),
                ("typmodin", _O), ("typmodout", _O), ("typanalyze", _O), ("typalign", _T), ("typstorage", _T),
                ("typnotnull", _B), ("typbasetype", _O), ("typtypmod", _I4), ("typndims", _I4),
                ("typcollation", _O), ("typdefaultbin", _T), ("typdefault", _T), ("typacl", "VARCHAR[]")],
    "pg_database": [("oid", _O), ("datname", _T), ("datdba", _O), ("encoding", _I4), ("datcollate", _T),
                    ("datctype", _T), ("datistemplate", _B), ("datallowconn", _B), ("datconnlimit", _I4),
                    ("datlastsysoid", _O), ("datfrozenxid", _O), ("datminmxid", _O), ("dattablespace", _O),
                    ("datacl", "VARCHAR[]")],
    "pg_roles": [("oid", _O), ("rolname", _T), ("rolsuper", _B), ("rolinherit", _B), ("rolcreaterole", _B),
                 ("rolcreatedb", _B), ("rolcanlogin", _B), ("rolreplication", _B), ("rolconnlimit", _I4),
                 ("rolpassword", _T), ("rolvaliduntil", "TIMESTAMP WITH TIME ZONE"), ("rolbypassrls", _B),
                 ("rolconfig", "VARCHAR[]")],
    "pg_user": [("usename", _T), ("usesysid", _O), ("usecreatedb", _B), ("usesuper", _B), ("userepl", _B),
                ("usebypassrls", _B), ("passwd", _T), ("valuntil", "TIMESTAMP WITH TIME ZONE"),
                ("useconfig", "VARCHAR[]")],
    "pg_settings": [("name", _T), ("setting", _T), ("unit", _T), ("category", _T), ("short_desc", _T),
                    ("extra_desc", _T), ("context", _T), ("vartype", _T), ("source", _T), ("min_val", _T),
                    ("max_val", _T), ("enumvals", "VARCHAR[]"), ("boot_val", _T), ("reset_val", _T),
                    ("sourcefile", _T), ("sourceline", _I4), ("pending_restart", _B)],
    "pg_description": [("objoid", _O), ("classoid", _O), ("objsubid", _I4), ("description", _T)],
    "pg_shdescription": [("objoid", _O), ("classoid", _O), ("description", _T)],
    "pg_attrdef": [("oid", _O), ("adrelid", _O), ("adnum", _I2), ("adbin", _T), ("adsrc", _T)],
    "pg_index": [("indexrelid", _O), ("indrelid", _O), ("indnatts", _I2), ("indnkeyatts", _I2),
                 ("indisunique", _B), ("indnullsnotdistinct", _B), ("indisprimary", _B), ("indisexclusion", _B),
                 ("indimmediate", _B), ("indisclustered", _B), ("indisvalid", _B), ("indcheckxmin", _B),
                 ("indisready", _B), ("indislive", _B), ("indisreplident", _B), ("indkey", "SMALLINT[]"),
                 ("indcollation", "UINTEGER[]"), ("indclass", "UINTEGER[]"), ("indoption", "SMALLINT[]"),
                 ("indexprs", _T), ("indpred", _T)],
    "pg_constraint": [("oid", _O), ("conname", _T), ("connamespace", _O), ("contype", _T), ("condeferrable", _B),
                      ("condeferred", _B), ("convalidated", _B), ("conrelid", _O), ("contypid", _O),
                      ("conindid", _O), ("conparentid", _O), ("confrelid", _O), ("confupdtype", _T),
                      ("confdeltype", _T), ("confmatchtype", _T), ("conislocal", _B), ("coninhcount", _I4),
                      ("connoinherit", _B), ("conkey", "SMALLINT[]"), ("confkey", "SMALLINT[]"),
                      ("conpfeqop", "UINTEGER[]"), ("conppeqop", "UINTEGER[]"), ("conffeqop", "UINTEGER[]"),
                      ("conexclop", "UINTEGER[]"), ("conbin", _T), ("consrc", _T)],
    "pg_proc": [("oid", _O), ("proname", _T), ("pronamespace", _O), ("proowner", _O), ("prolang", _O),
                ("procost", _F4), ("prorows", _F4), ("provariadic", _O), ("prosupport", _T), ("prokind", _T),
                ("prosecdef", _B), ("proleakproof", _B), ("proisstrict", _B), ("proretset", _B),
                ("provolatile", _T), ("proparallel", _T), ("pronargs", _I2), ("pronargdefaults", _I2),
                ("prorettype", _O), ("proargtypes", "UINTEGER[]"), ("proallargtypes", "UINTEGER[]"),
                ("proargmodes", "VARCHAR[]"), ("proargnames", "VARCHAR[]"), ("proargdefaults", _T),
                ("protrftypes", "UINTEGER[]"), ("prosrc", _T), ("probin", _T), ("proconfig", "VARCHAR[]"),
                ("proacl", "VARCHAR[]"), ("proisagg", _B), ("proiswindow", _B)],
    "pg_depend": [("classid", _O), ("objid", _O), ("objsubid", _I4), ("refclassid", _O), ("refobjid", _O),
                  ("refobjsubid", _I4), ("deptype", _T)],
    "pg_inherits": [("inhrelid", _O), ("inhparent", _O), ("inhseqno", _I4), ("inhdetachpending", _B)],
    "pg_am": [("oid", _O), ("amname", _T), ("amhandler", _T), ("amtype", _T)],
    "pg_tablespace": [("oid", _O), ("spcname", _T), ("spcowner", _O), ("spcacl", "VARCHAR[]"),
                      ("spcoptions", "VARCHAR[]")],
    "pg_extension": [("oid", _O), ("extname", _T), ("extowner", _O), ("extnamespace", _O), ("extrelocatable", _B),
                     ("extversion", _T), ("extconfig", "UINTEGER[]"), ("extcondition", "VARCHAR[]")],
    "pg_trigger": [("oid", _O), ("tgrelid", _O), ("tgparentid", _O), ("tgname", _T), ("tgfoid", _O),
                   ("tgtype", _I2), ("tgenabled", _T), ("tgisinternal", _B), ("tgconstrrelid", _O),
                   ("tgconstrindid", _O), ("tgconstraint", _O), ("tgdeferrable", _B), ("tginitdeferred", _B),
                   ("tgnargs", _I2), ("tgattr", "SMALLINT[]"), ("tgargs", _T), ("tgqual", _T),
                   ("tgoldtable", _T), ("tgnewtable", _T)],
    "pg_rewrite": [("oid", _O), ("rulename", _T), ("ev_class", _O), ("ev_type", _T), ("ev_enabled", _T),
                   ("is_instead", _B), ("ev_qual", _T), ("ev_action", _T)],
    "pg_collation": [("oid", _O), ("collname", _T), ("collnamespace", _O), ("collowner", _O),
                     ("collprovider", _T), ("collisdeterministic", _B), ("collencoding", _I4),
                     ("collcollate", _T), ("collctype", _T), ("collversion", _T)],
    "pg_language": [("oid", _O), ("lanname", _T), ("lanowner", _O), ("lanispl", _B), ("lanpltrusted", _B),
                    ("lanplcallfoid", _O), ("laninline", _O), ("lanvalidator", _O), ("lanacl", "VARCHAR[]")],
    "pg_enum": [("oid", _O), ("enumtypid", _O), ("enumsortorder", _F4), ("enumlabel", _T)],
    "pg_range": [("rngtypid", _O), ("rngsubtype", _O), ("rngmultitypid", _O), ("rngcollation", _O),
                 ("rngsubopc", _O), ("rngcanonical", _T), ("rngsubdiff", _T)],
    "pg_operator": [("oid", _O), ("oprname", _T), ("oprnamespace", _O), ("oprowner", _O), ("oprkind", _T),
                    ("oprcanmerge", _B), ("oprcanhash", _B), ("oprleft", _O), ("oprright", _O),
                    ("oprresult", _O), ("oprcom", _O), ("oprnegate", _O), ("oprcode", _T), ("oprrest", _T),
                    ("oprjoin", _T)],
    "pg_opclass": [("oid", _O), ("opcmethod", _O), ("opcname", _T), ("opcnamespace", _O), ("opcowner", _O),
                   ("opcfamily", _O), ("opcintype", _O), ("opcdefault", _B), ("opckeytype", _O)],
    "pg_cast": [("oid", _O), ("castsource", _O), ("casttarget", _O), ("castfunc", _O), ("castcontext", _T),
                ("castmethod", _T)],
    "pg_aggregate": [("aggfnoid", _O), ("aggkind", _T), ("aggnumdirectargs", _I2), ("aggtransfn", _T),
                     ("aggfinalfn", _T), ("aggtranstype", _O), ("agginitval", _T)],
    "pg_foreign_table": [("ftrelid", _O), ("ftserver", _O), ("ftoptions", "VARCHAR[]")],
    "pg_foreign_server": [("oid", _O), ("srvname", _T), ("srvowner", _O), ("srvfdw", _O), ("srvtype", _T),
                          ("srvversion", _T), ("srvacl", "VARCHAR[]"), ("srvoptions", "VARCHAR[]")],
    "pg_foreign_data_wrapper": [("oid", _O), ("fdwname", _T), ("fdwowner", _O), ("fdwhandler", _O),
                                ("fdwvalidator", _O), ("fdwacl", "VARCHAR[]"), ("fdwoptions", "VARCHAR[]")],
    "pg_user_mapping": [("oid", _O), ("umuser", _O), ("umserver", _O), ("umoptions", "VARCHAR[]")],
    "pg_event_trigger": [("oid", _O), ("evtname", _T), ("evtevent", _T), ("evtowner", _O), ("evtfoid", _O),
                         ("evtenabled", _T), ("evttags", "VARCHAR[]")],
    "pg_policy": [("oid", _O), ("polname", _T), ("polrelid", _O), ("polcmd", _T), ("polpermissive", _B),
                  ("polroles", "UINTEGER[]"), ("polqual", _T), ("polwithcheck", _T)],
    "pg_sequence": [("seqrelid", _O), ("seqtypid", _O), ("seqstart", _I8), ("seqincrement", _I8),
                    ("seqmax", _I8), ("seqmin", _I8), ("seqcache", _I8), ("seqcycle", _B)],
    "pg_statistic_ext": [("oid", _O), ("stxrelid", _O), ("stxname", _T), ("stxnamespace", _O),
                         ("stxowner", _O), ("stxstattarget", _I4), ("stxkeys", "SMALLINT[]"),
                         ("stxkind", "VARCHAR[]"), ("stxexprs", _T)],
    "pg_partitioned_table": [("partrelid", _O), ("partstrat", _T), ("partnatts", _I2), ("partdefid", _O),
                             ("partattrs", "SMALLINT[]")],
    "pg_publication": [("oid", _O), ("pubname", _T), ("pubowner", _O), ("puballtables", _B), ("pubinsert", _B),
                       ("pubupdate", _B), ("pubdelete", _B), ("pubtruncate", _B), ("pubviaroot", _B)],
    "pg_publication_rel": [("oid", _O), ("prpubid", _O), ("prrelid", _O), ("prqual", _T), ("prattrs", "SMALLINT[]")],
    "pg_publication_namespace": [("oid", _O), ("pnpubid", _O), ("pnnspid", _O)],
    "pg_subscription": [("oid", _O), ("subname", _T), ("subowner", _O), ("subenabled", _B)],
    "pg_auth_members": [("roleid", _O), ("member", _O), ("grantor", _O), ("admin_option", _B)],
    "pg_init_privs": [("objoid", _O), ("classoid", _O), ("objsubid", _I4), ("privtype", _T),
                      ("initprivs", "VARCHAR[]")],
    "pg_default_acl": [("oid", _O), ("defaclrole", _O), ("defaclnamespace", _O), ("defaclobjtype", _T),
                       ("defaclacl", "VARCHAR[]")],
    "pg_db_role_setting": [("setdatabase", _O), ("setrole", _O), ("setconfig", "VARCHAR[]")],
    "pg_conversion": [("oid", _O), ("conname", _T), ("connamespace", _O)],
    "pg_statistic": [("starelid", _O), ("staattnum", _I2), ("stanullfrac", _F4)],
    "pg_seclabel": [("objoid", _O), ("classoid", _O), ("objsubid", _I4), ("provider", _T), ("label", _T)],
    "pg_transform": [("oid", _O), ("trftype", _O), ("trflang", _O)],
    "pg_largeobject_metadata": [("oid", _O), ("lomowner", _O), ("lomacl", "VARCHAR[]")],
    "pg_ts_config": [("oid", _O), ("cfgname", _T), ("cfgnamespace", _O)],
    "pg_stat_activity": [("datid", _O), ("datname", _T), ("pid", _I4), ("usesysid", _O), ("usename", _T),
                         ("application_name", _T), ("client_addr", _T), ("client_port", _I4),
                         ("backend_start", "TIMESTAMP WITH TIME ZONE"), ("xact_start", "TIMESTAMP WITH TIME ZONE"),
                         ("query_start", "TIMESTAMP WITH TIME ZONE"), ("state_change", "TIMESTAMP WITH TIME ZONE"),
                         ("wait_event_type", _T), ("wait_event", _T), ("state", _T), ("backend_xid", _O),
                         ("backend_xmin", _O), ("query", _T), ("backend_type", _T)],
    "pg_locks": [("locktype", _T), ("database", _O), ("relation", _O), ("pid", _I4), ("mode", _T),
                 ("granted", _B)],
    "pg_stat_user_tables": [("relid", _O), ("schemaname", _T), ("relname", _T), ("n_live_tup", _I8)],
}
_AUTHID_ALIAS = "pg_authid"  # a view over pg_roles
#: views over the tables above: name → SELECT
VIEWS = {
    "pg_authid": "SELECT oid, rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolcanlogin, rolreplication, "
                 "rolbypassrls, rolconnlimit, rolpassword, rolvaliduntil FROM {s}.pg_roles",
    "pg_tables": "SELECT n.nspname AS schemaname, c.relname AS tablename, 'duckduck' AS tableowner, "
                 "NULL::VARCHAR AS tablespace, false AS hasindexes, false AS hasrules, false AS hastriggers, "
                 "false AS rowsecurity FROM {s}.pg_class c JOIN {s}.pg_namespace n ON n.oid = c.relnamespace "
                 "WHERE c.relkind IN ('r', 'p')",
    "pg_views": "SELECT n.nspname AS schemaname, c.relname AS viewname, 'duckduck' AS viewowner, "
                "d.definition FROM {s}.pg_class c JOIN {s}.pg_namespace n ON n.oid = c.relnamespace "
                "LEFT JOIN {s}.duckduck_viewdefs d ON d.oid = c.oid WHERE c.relkind = 'v'",
    "pg_matviews": "SELECT NULL::VARCHAR AS schemaname, NULL::VARCHAR AS matviewname, NULL::VARCHAR AS matviewowner, "
                   "NULL::VARCHAR AS tablespace, false AS hasindexes, false AS ispopulated, NULL::VARCHAR AS definition "
                   "WHERE false",
    "pg_indexes": "SELECT NULL::VARCHAR AS schemaname, NULL::VARCHAR AS tablename, NULL::VARCHAR AS indexname, "
                  "NULL::VARCHAR AS tablespace, NULL::VARCHAR AS indexdef WHERE false",
}

_MACROS = [
    "format_type(t, m) AS CASE WHEN t = 1700 AND m >= 4 THEN 'numeric(' || ((m - 4) >> 16) || ',' || ((m - 4) & 65535) "
    "|| ')' ELSE coalesce((SELECT name FROM {s}.duckduck_format_names f WHERE f.oid = t), 'text') END",
    "pg_get_expr(e, r) AS NULL::VARCHAR, (e, r, p) AS NULL::VARCHAR",
    "obj_description(o) AS (SELECT description FROM {s}.pg_description d WHERE d.objoid = o AND d.objsubid = 0 LIMIT 1), "
    "(o, c) AS (SELECT description FROM {s}.pg_description d WHERE d.objoid = o AND d.objsubid = 0 LIMIT 1)",
    "col_description(o, n) AS (SELECT description FROM {s}.pg_description d WHERE d.objoid = o AND d.objsubid = n LIMIT 1)",
    "shobj_description(o, c) AS NULL::VARCHAR",
    "pg_get_userbyid(o) AS 'duckduck'",
    "pg_encoding_to_char(e) AS 'UTF8'",
    "pg_client_encoding() AS 'UTF8'",
    "pg_get_constraintdef(o) AS NULL::VARCHAR, (o, p) AS NULL::VARCHAR",
    "pg_get_indexdef(o) AS NULL::VARCHAR, (o, c, p) AS NULL::VARCHAR",
    "pg_get_viewdef(o) AS (SELECT definition FROM {s}.duckduck_viewdefs v WHERE v.oid = o), "
    "(o, p) AS (SELECT definition FROM {s}.duckduck_viewdefs v WHERE v.oid = o)",
    "pg_get_triggerdef(o) AS NULL::VARCHAR, (o, p) AS NULL::VARCHAR",
    "pg_get_ruledef(o) AS NULL::VARCHAR, (o, p) AS NULL::VARCHAR",
    "pg_get_functiondef(o) AS NULL::VARCHAR",
    "pg_get_function_result(o) AS NULL::VARCHAR",
    "pg_get_function_arguments(o) AS ''",
    "pg_get_function_identity_arguments(o) AS ''",
    "pg_get_serial_sequence(t, c) AS NULL::VARCHAR",
    "pg_get_partkeydef(o) AS NULL::VARCHAR",
    "pg_get_statisticsobjdef(o) AS NULL::VARCHAR",
    "pg_get_statisticsobjdef_columns(o) AS NULL::VARCHAR",
    "pg_table_is_visible(o) AS true",
    "pg_relation_is_publishable(o) AS false",
    "pg_type_is_visible(o) AS true",
    "pg_function_is_visible(o) AS true",
    "has_table_privilege(a, b) AS true, (a, b, c) AS true",
    "has_schema_privilege(a, b) AS true, (a, b, c) AS true",
    "has_database_privilege(a, b) AS true, (a, b, c) AS true",
    "has_column_privilege(a, b, c) AS true, (a, b, c, d) AS true",
    "has_any_column_privilege(a, b) AS true, (a, b, c) AS true",
    "has_function_privilege(a, b) AS true, (a, b, c) AS true",
    "has_sequence_privilege(a, b) AS true, (a, b, c) AS true",
    "pg_has_role(a, b) AS true, (a, b, c) AS true",
    "current_setting(n) AS (SELECT setting FROM {s}.pg_settings s WHERE lower(s.name) = lower(n)), "
    "(n, m) AS (SELECT setting FROM {s}.pg_settings s WHERE lower(s.name) = lower(n))",
    "version() AS 'PostgreSQL " + SERVER_VERSION + " (duckduck) on x86_64-pc-linux-gnu, compiled by duckduck, 64-bit'",
    "pg_backend_pid() AS 1",
    "txid_current() AS 1",
    "pg_is_in_recovery() AS false",
    "pg_postmaster_start_time() AS (SELECT started FROM {s}.duckduck_server)",
    "pg_conf_load_time() AS (SELECT started FROM {s}.duckduck_server)",
    "pg_total_relation_size(o) AS 0",
    "pg_relation_size(o) AS 0, (o, f) AS 0",
    "pg_table_size(o) AS 0",
    "pg_indexes_size(o) AS 0",
    "pg_database_size(d) AS 0",
    "pg_tablespace_location(o) AS ''",
    "pg_stat_get_numscans(o) AS 0",
    "pg_stat_get_tuples_returned(o) AS 0",
    "pg_stat_get_tuples_fetched(o) AS 0",
    "pg_stat_get_live_tuples(o) AS 0",
    "pg_stat_get_dead_tuples(o) AS 0",
    "pg_stat_get_last_vacuum_time(o) AS NULL::TIMESTAMP WITH TIME ZONE",
    "pg_stat_get_last_analyze_time(o) AS NULL::TIMESTAMP WITH TIME ZONE",
    "pg_current_xact_id() AS 1",
    "txid_snapshot_xmin(s) AS 1",
    "txid_current_snapshot() AS '1:1:'",
    "pg_get_function_sqlbody(o) AS NULL::VARCHAR",
    "to_regclass(t) AS (SELECT oid FROM {s}.pg_class c WHERE c.relname = split_part(t, '.', -1) LIMIT 1)",
    "current_database() AS (SELECT datname FROM {s}.duckduck_server)",
    "current_schema() AS coalesce(getvariable('duckduck_schema'), 'public')",
    "current_user_() AS getvariable('duckduck_user')",
    "session_user_() AS getvariable('duckduck_user')",
    "set_config(n, v, l) AS v",
    "current_schemas(b) AS CASE WHEN b THEN ['pg_catalog', coalesce(getvariable('duckduck_schema'), 'public')] "
    "ELSE [coalesce(getvariable('duckduck_schema'), 'public')] END",
    "quote_ident(t) AS CASE WHEN regexp_full_match(t, '[a-z_][a-z0-9_$]*') THEN t "
    "ELSE '\"' || replace(t, '\"', '\"\"') || '\"' END",
    "array_upper(a, d) AS CASE WHEN len(a) > 0 THEN len(a) END",
    "array_lower(a, d) AS CASE WHEN len(a) > 0 THEN 1 END",
    "pg_get_keywords() AS TABLE SELECT keyword_name AS word, "
    "CASE keyword_category WHEN 'reserved' THEN 'R' WHEN 'unreserved' THEN 'U' WHEN 'type_function' THEN 'T' ELSE 'C' END "
    "AS catcode, keyword_category AS catdesc FROM duckdb_keywords()",
]
_WITH_XMIN = ("pg_namespace", "pg_class", "pg_type", "pg_proc", "pg_attribute", "pg_constraint", "pg_index",
              "pg_trigger", "pg_roles", "pg_database", "pg_description", "pg_extension", "pg_language",
              "pg_collation", "pg_operator", "pg_tablespace", "pg_am", "pg_cast", "pg_depend", "pg_rewrite",
              "pg_attrdef", "pg_inherits", "pg_enum")
MACRO_NAMES = {re.match(r"\w+", m).group(0) for m in _MACROS}
OBJECT_NAMES = set(TABLES) | set(VIEWS)
INFO_VIEWS = ("schemata", "tables", "columns", "views", "_pg_expandarray")


def _oid(text: str, taken: set) -> int:
    """A stable oid for a name — the same across restarts, so a client's saved references keep working."""
    oid = 16384 + zlib.crc32(text.encode("utf-8")) % 2_000_000_000
    while oid in taken:
        oid += 1
    taken.add(oid)
    return oid


class ColumnMemory:
    """The columns duckduck has seen each table return, kept in a JSON file between runs."""

    def __init__(self, path: Optional[str] = None):
        self.path = path
        self._lock = threading.Lock()
        self.version = 0
        self.columns: Dict[str, List[Tuple[str, str]]] = {}
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
                self.columns = {k: [tuple(c) for c in v] for k, v in (data.get("tables") or {}).items()}
            except (OSError, ValueError, AttributeError, TypeError) as exc:
                logger.warning("couldn't read the known columns from %s: %s", path, exc)

    def learn(self, table: str, columns: Sequence[Tuple[str, str]]) -> None:
        columns = [(str(n), str(t)) for n, t in columns]
        if not columns:
            return
        with self._lock:
            if self.columns.get(table) == columns:
                return
            self.columns[table] = columns
            self.version += 1
            self._save()

    def _save(self) -> None:
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"tables": self.columns}, fh, indent=1, sort_keys=True)
            os.replace(tmp, self.path)
        except OSError as exc:
            logger.warning("couldn't save the known columns to %s: %s", self.path, exc)

    def get(self, table: str) -> Optional[List[Tuple[str, str]]]:
        with self._lock:
            return list(self.columns.get(table) or []) or None


def needs_text(needs: Sequence[Tuple[str, ...]]) -> str:
    """How a client gives a table its arguments: ``Needs table_name: WHERE table_name = '…'``."""
    which = "; ".join(" or ".join(group) for group in needs)
    where = " AND ".join(f"{group[-1]} = '…'" for group in needs)
    return f"Needs {which}: WHERE {where} (or arg.{needs[0][-1]} = '…')"


def table_entries(duck: Any, columns: ColumnMemory, behind: Sequence[Dict[str, Any]] = ()) -> List[Dict[str, Any]]:
    """Every table a client can select from without arguments: schema, name, the function behind it, columns.

    ``behind``: the tables behind connectors' catalogs (``DuckAPI.nested_tables`` rows) — listed by their
    address (``servicenow.incident``, ``s3_data.security.proxy_logs``) unless a table of that name is already
    listed; their "function" is that address, which any query resolves to the table function's call."""
    from .. import addresses
    from ..common.kinds import kind_of, needed_arguments, required_params

    out = []
    known = addresses.services(duck)
    for name, fn in list(duck.functions.items()):
        try:
            kind = kind_of(fn)
            needs = [(p.name,) for p in required_params(fn)] + [tuple(g) for g in needed_arguments(fn)]
        except Exception:  # noqa: BLE001 — an odd callable isn't worth failing the catalog
            continue
        if kind not in ("table", "catalog", "table function"):
            continue
        try:
            address = None if kind == "table function" else addresses.address_of(duck, name)
        except Exception:  # noqa: BLE001
            address = None
        parts = addresses._parts(address) if address else []
        if len(parts) >= 3 and parts[1].lower() == addresses.default_database(duck).lower():
            parts = [parts[0]] + parts[2:]  # sharepoint.duckdefault.x is sharepoint's own x: schema sharepoint
        if len(parts) >= 2:
            schema, table = ".".join(parts[:-1]), parts[-1]
        else:  # no address — a table function (svc.x would mean svc's table x): its connector's schema anyway
            service = duck.service_of.get(name)
            prefix = known.get(service) if service else None
            if service in known and prefix and name.startswith(prefix + "_"):
                schema, table = service, name[len(prefix) + 1:]
            elif service in known and not prefix:
                schema, table = service, name
            else:
                schema, table = "public", name
        view = (getattr(duck, "views", None) or {}).get((getattr(duck, "view_key", None) or {}).get(name, name)) or {}
        try:
            description = view.get("description") or duck._describe_function(fn) or None
        except Exception:  # noqa: BLE001
            description = None
        if needs:
            description = needs_text(needs) + (f" — {description}" if description else "")
        out.append({"function": name, "schema": schema, "table": table, "kind": kind,
                    "view_sql": view.get("sql"), "description": description,
                    "columns": columns.get(name), "needs": needs})
    listed = {(e["schema"].lower(), e["table"].lower()) for e in out}
    default = addresses.default_database(duck).lower()
    for row in behind:
        address = row.get("address")
        parts = addresses._parts(address) if address else []
        if len(parts) < 2:
            continue
        if len(parts) >= 3 and parts[1].lower() == default:
            parts = [parts[0]] + parts[2:]
        schema, table = ".".join(parts[:-1]), parts[-1]
        if (schema.lower(), table.lower()) in listed:
            continue  # a saved table (or a table of its own) already has this name
        listed.add((schema.lower(), table.lower()))
        call = ", ".join(f"{k}='{v}'" for k, v in (row.get("args") or {}).items())
        out.append({"function": address, "schema": schema, "table": table, "kind": "table", "view_sql": None,
                    "description": f"{row.get('table')}({call}) — listed by {row.get('catalog')}",
                    "columns": columns.get(address), "needs": []})
    out.sort(key=lambda e: (e["schema"], e["table"]))
    return out


class PgCatalog:
    """The ``duckduck_pg`` schema: built from the registered tables, rebuilt when they change."""

    def __init__(self, conn: Any, duck: Any, columns: Optional[ColumnMemory] = None, database: str = "duckduck",
                 settings: Optional[Dict[str, str]] = None):
        self.conn = conn
        self.duck = duck
        self.columns = columns or ColumnMemory()
        self.database = database
        self.settings = settings or {}
        self._lock = threading.Lock()
        self._signature: Any = None
        self.system_oids = dict(_SYSTEM_OIDS)
        #: (schema, table) → oid, and oid → entry; filled by ``refresh``
        self.oids: Dict[Tuple[str, str], int] = {}
        self.namespaces: Dict[str, int] = {}
        self.started = time.time()
        #: the tables behind connectors' catalogs (``PGServer.read_catalogs``), listed next to the rest
        self.behind: List[Dict[str, Any]] = []
        self.behind_version = 0
        self._create()

    # -- building --------------------------------------------------------------------------------------

    def _create(self) -> None:
        c = self.conn
        s = SCHEMA
        c.execute(f"CREATE SCHEMA IF NOT EXISTS {s}")
        c.execute(f"CREATE SCHEMA IF NOT EXISTS {INFO_SCHEMA}")
        for name, cols in TABLES.items():
            c.execute(f"CREATE OR REPLACE TABLE {s}.{name} ({', '.join(f'{n} {t}' for n, t in cols)})")
        c.execute(f"CREATE OR REPLACE TABLE {s}.duckduck_viewdefs (oid UINTEGER, definition VARCHAR)")
        c.execute(f"CREATE OR REPLACE TABLE {s}.duckduck_format_names (oid UINTEGER, name VARCHAR)")
        c.execute(f"CREATE OR REPLACE TABLE {s}.duckduck_server (datname VARCHAR, started TIMESTAMP WITH TIME ZONE)")
        c.execute(f"INSERT INTO {s}.duckduck_server VALUES (?, to_timestamp(?))", [self.database, self.started])
        for name, select in VIEWS.items():
            c.execute(f"CREATE OR REPLACE VIEW {s}.{name} AS {select.format(s=s)}")
        for macro in _MACROS:
            c.execute(f"CREATE OR REPLACE MACRO {s}.{macro.format(s=s)}")
        self._static()
        for name in _WITH_XMIN:  # PostgreSQL's system column: DataGrip reads it as a state number (not in *)
            c.execute(f"ALTER TABLE {s}.{name} ADD COLUMN xmin UINTEGER DEFAULT 1")
        self._info_views()

    def _static(self) -> None:
        c, s = self.conn, SCHEMA
        rows = []
        procs = self.procs = {}  # the types' I/O functions: pg_type.typreceive = pg_proc.oid (Npgsql finds arrays so)
        for name in sorted({f"{'array' if pg.ELEMENT_OF.get(o) else t[0]}_{kind}" for o, t in pg.TYPES.items()
                            for kind in ("in", "out", "recv", "send")}):
            procs[name] = _PROC_BASE + len(procs)
        for oid, (name, length, category) in sorted(pg.TYPES.items()):
            array = pg.ARRAY_OF.get(oid, 0)
            element = pg.ELEMENT_OF.get(oid, 0)
            byval = length in (1, 2, 4, 8)
            io = "array" if element else name
            rows.append((oid, name, _NS_CATALOG, _OWNER, length, byval, "p" if oid == pg.UNKNOWN else "b", category,
                         False, True, ",", 0, "array_subscript_handler" if element else "-", element, array,
                         procs[f"{io}_in"], procs[f"{io}_out"], procs[f"{io}_recv"], procs[f"{io}_send"], 0, 0, 0,
                         {1: "c", 2: "s", 4: "i", 8: "d"}.get(length, "i"), "p" if byval else "x", False, 0, -1,
                         0, 100 if category == "S" else 0, None, None, None))
        c.executemany(f"INSERT INTO {s}.pg_type VALUES ({', '.join('?' * len(TABLES['pg_type']))})", rows)
        c.executemany(f"INSERT INTO {s}.pg_proc (oid, proname, pronamespace, proowner, prolang, procost, prorows, "
                      f"provariadic, prokind, prosecdef, proleakproof, proisstrict, proretset, provolatile, "
                      f"proparallel, pronargs, pronargdefaults, prorettype, proisagg, proiswindow) "
                      f"VALUES (?, ?, {_NS_CATALOG}, {_OWNER}, 12, 1, 0, 0, 'f', false, false, true, false, 'i', "
                      f"'s', 1, 0, 0, false, false)", [(oid, name) for name, oid in procs.items()])
        c.executemany(f"INSERT INTO {s}.duckduck_format_names VALUES (?, ?)",
                      [(oid, pg.format_type(oid)) for oid in pg.TYPES])
        c.execute(f"INSERT INTO {s}.pg_database VALUES (16384, ?, {_OWNER}, 6, 'C', 'C', false, true, -1, 16383, 0, 1, "
                  f"1663, NULL)", [self.database])
        c.execute(f"INSERT INTO {s}.pg_roles VALUES ({_OWNER}, 'duckduck', false, true, false, false, true, false, "
                  f"-1, '********', NULL, false, NULL)")
        c.execute(f"INSERT INTO {s}.pg_user VALUES ('duckduck', {_OWNER}, false, false, false, false, '********', "
                  f"NULL, NULL)")
        c.execute(f"INSERT INTO {s}.pg_am VALUES (2, 'heap', 'heap_tableam_handler', 't'), "
                  f"(403, 'btree', 'bthandler', 'i')")
        c.execute(f"INSERT INTO {s}.pg_tablespace VALUES (1663, 'pg_default', {_OWNER}, NULL, NULL)")
        c.execute(f"INSERT INTO {s}.pg_language VALUES (12, 'internal', {_OWNER}, false, false, 0, 0, 0, NULL), "
                  f"(14, 'sql', {_OWNER}, false, true, 0, 0, 0, NULL)")
        c.execute(f"INSERT INTO {s}.pg_collation VALUES (100, 'default', 11, {_OWNER}, 'd', true, -1, NULL, NULL, NULL), "
                  f"(950, 'C', 11, {_OWNER}, 'c', true, -1, 'C', 'C', NULL)")
        settings = {
            "server_version": SERVER_VERSION, "server_version_num": "140000", "search_path": '"$user", public',
            "standard_conforming_strings": "on", "client_encoding": "UTF8", "server_encoding": "UTF8",
            "DateStyle": "ISO, MDY", "TimeZone": "UTC", "integer_datetimes": "on", "max_identifier_length": "63",
            "transaction_isolation": "read committed", "default_transaction_isolation": "read committed",
            "transaction_read_only": "on", "default_transaction_read_only": "on", "lc_collate": "C",
            "lc_ctype": "C", "max_index_keys": "32", "block_size": "8192", "IntervalStyle": "postgres",
            "is_superuser": "off", "extra_float_digits": "3", "statement_timeout": "0", "lock_timeout": "0",
            "idle_in_transaction_session_timeout": "0", "bytea_output": "hex", "max_connections": "100",
            "application_name": "", "ssl": "off", "row_security": "on", "search_path_display": "public",
            **self.settings,
        }
        c.executemany(f"INSERT INTO {s}.pg_settings VALUES (?, ?, NULL, 'duckduck', ?, NULL, 'user', 'string', "
                      f"'default', NULL, NULL, NULL, ?, ?, NULL, NULL, false)",
                      [(k, v, k, v, v) for k, v in settings.items()])

    def _info_views(self) -> None:
        c, s, i = self.conn, SCHEMA, INFO_SCHEMA
        # pgjdbc's getPrimaryKeys / getIndexInfo: (information_schema._pg_expandarray(i.indkey)).n — no index exists here
        c.execute(f"CREATE OR REPLACE MACRO {i}._pg_expandarray(a) AS {{'x': a[1], 'n': 1}}")
        c.execute(f"""CREATE OR REPLACE VIEW {i}.schemata AS SELECT ? AS catalog_name, nspname AS schema_name,
            'duckduck' AS schema_owner, NULL::VARCHAR AS default_character_set_catalog,
            NULL::VARCHAR AS default_character_set_schema, NULL::VARCHAR AS default_character_set_name,
            NULL::VARCHAR AS sql_path FROM {s}.pg_namespace""".replace("?", f"'{self.database}'"))
        c.execute(f"""CREATE OR REPLACE VIEW {i}.tables AS SELECT '{self.database}' AS table_catalog,
            n.nspname AS table_schema, c.relname AS table_name,
            CASE c.relkind WHEN 'v' THEN 'VIEW' ELSE 'BASE TABLE' END AS table_type,
            NULL::VARCHAR AS self_referencing_column_name, NULL::VARCHAR AS reference_generation,
            NULL::VARCHAR AS user_defined_type_catalog, NULL::VARCHAR AS user_defined_type_schema,
            NULL::VARCHAR AS user_defined_type_name, 'NO' AS is_insertable_into, 'NO' AS is_typed,
            NULL::VARCHAR AS commit_action
            FROM {s}.pg_class c JOIN {s}.pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r', 'v') AND n.nspname NOT IN ('pg_catalog', 'information_schema')""")
        c.execute(f"""CREATE OR REPLACE VIEW {i}.columns AS SELECT '{self.database}' AS table_catalog,
            n.nspname AS table_schema, c.relname AS table_name, a.attname AS column_name,
            a.attnum::INTEGER AS ordinal_position, NULL::VARCHAR AS column_default, 'YES' AS is_nullable,
            {s}.format_type(a.atttypid, a.atttypmod) AS data_type,
            NULL::INTEGER AS character_maximum_length, NULL::INTEGER AS character_octet_length,
            NULL::INTEGER AS numeric_precision, NULL::INTEGER AS numeric_precision_radix,
            NULL::INTEGER AS numeric_scale, NULL::INTEGER AS datetime_precision,
            NULL::VARCHAR AS interval_type, NULL::INTEGER AS interval_precision,
            NULL::VARCHAR AS character_set_catalog, NULL::VARCHAR AS character_set_schema,
            NULL::VARCHAR AS character_set_name, NULL::VARCHAR AS collation_catalog,
            NULL::VARCHAR AS collation_schema, NULL::VARCHAR AS collation_name,
            NULL::VARCHAR AS domain_catalog, NULL::VARCHAR AS domain_schema, NULL::VARCHAR AS domain_name,
            '{self.database}' AS udt_catalog, 'pg_catalog' AS udt_schema, t.typname AS udt_name,
            NULL::VARCHAR AS scope_catalog, NULL::VARCHAR AS scope_schema, NULL::VARCHAR AS scope_name,
            NULL::INTEGER AS maximum_cardinality, a.attnum::VARCHAR AS dtd_identifier,
            'NO' AS is_self_referencing, 'NO' AS is_identity, NULL::VARCHAR AS identity_generation,
            NULL::VARCHAR AS identity_start, NULL::VARCHAR AS identity_increment,
            NULL::VARCHAR AS identity_maximum, NULL::VARCHAR AS identity_minimum,
            NULL::VARCHAR AS identity_cycle, 'NEVER' AS is_generated, NULL::VARCHAR AS generation_expression,
            'NO' AS is_updatable
            FROM {s}.pg_attribute a JOIN {s}.pg_class c ON c.oid = a.attrelid
            JOIN {s}.pg_namespace n ON n.oid = c.relnamespace LEFT JOIN {s}.pg_type t ON t.oid = a.atttypid
            WHERE a.attnum > 0 AND n.nspname NOT IN ('pg_catalog', 'information_schema')""")
        c.execute(f"""CREATE OR REPLACE VIEW {i}.views AS SELECT '{self.database}' AS table_catalog,
            schemaname AS table_schema, viewname AS table_name, definition AS view_definition,
            'NONE' AS check_option, 'NO' AS is_updatable, 'NO' AS is_insertable_into,
            'NO' AS is_trigger_updatable, 'NO' AS is_trigger_deletable, 'NO' AS is_trigger_insertable_into
            FROM {s}.pg_views""")

    def signature(self) -> Any:
        duck = self.duck
        return (tuple(sorted(duck.functions)), self.columns.version, len(getattr(duck, "views", {}) or {}),
                tuple(sorted((getattr(duck, "view_key", None) or {}).items())), self.behind_version)

    def refresh(self, force: bool = False) -> bool:
        """Rebuilds namespaces, tables, columns and descriptions when the registered tables changed. True if it did."""
        sig = self.signature()
        if not force and sig == self._signature:
            return False
        with self._lock:
            if not force and sig == self._signature:
                return False
            started = time.perf_counter()
            entries = table_entries(self.duck, self.columns, self.behind)
            self._fill(entries)
            self._signature = sig
            logger.info("PostgreSQL catalog: %d tables in %d schemas (%.2fs)", len(entries), len(self.namespaces),
                        time.perf_counter() - started)
            return True

    def _fill(self, entries: List[Dict[str, Any]]) -> None:
        import pandas as pd

        taken: set = set(pg.TYPES) | set(self.system_oids.values()) | {_NS_CATALOG, _NS_PUBLIC, _NS_INFO, 16384}
        taken |= set(self.procs.values())
        namespaces = {"pg_catalog": _NS_CATALOG, "public": _NS_PUBLIC, "information_schema": _NS_INFO}
        for e in entries:
            if e["schema"] not in namespaces:
                namespaces[e["schema"]] = _oid("namespace:" + e["schema"], taken)
        classes, attrs, descriptions, viewdefs, oids = [], [], [], [], {}
        for name, oid in self.system_oids.items():
            classes.append(self._class_row(oid, name, _NS_CATALOG, "v" if name in VIEWS else "r", 0))
        for e in entries:
            oid = _oid(f"table:{e['schema']}.{e['table']}", taken)
            oids[(e["schema"], e["table"])] = oid
            e["oid"] = oid
            cols = e["columns"] or [(UNKNOWN_COLUMN, "VARCHAR")]
            kind = "v" if e.get("view_sql") else "r"
            classes.append(self._class_row(oid, e["table"], namespaces[e["schema"]], kind, len(cols)))
            for i, (col, dtype) in enumerate(cols, start=1):
                t = pg.oid_of(dtype)
                size = pg.TYPES.get(t, ("", -1, ""))[1]
                attrs.append((oid, col, t, -1, size, i, 1 if t in pg.ELEMENT_OF else 0, -1, pg.typmod_of(dtype),
                              size in (1, 2, 4, 8), "p" if size in (1, 2, 4, 8) else "x", "i", False, False, False,
                              "", "", False, "", True, 0, 100 if t in (pg.TEXT, pg.VARCHAR) else 0, None, None, None, None))
            if e["description"]:
                descriptions.append((oid, 1259, 0, e["description"]))
            if not e["columns"]:
                descriptions.append((oid, 1259, 1, "duckduck learns this table's columns the first time a query "
                                                   "reads it — run SELECT * FROM it once, then refresh"))
            if e.get("view_sql"):
                viewdefs.append((oid, e["view_sql"]))
        c, s = self.conn, SCHEMA
        frames = {
            "pg_namespace": pd.DataFrame([(oid, n, _OWNER, None) for n, oid in namespaces.items()],
                                         columns=[x for x, _ in TABLES["pg_namespace"]]),
            "pg_class": pd.DataFrame(classes, columns=[x for x, _ in TABLES["pg_class"]]),
            "pg_attribute": pd.DataFrame(attrs, columns=[x for x, _ in TABLES["pg_attribute"]]),
            "pg_description": pd.DataFrame(descriptions, columns=[x for x, _ in TABLES["pg_description"]]),
            "duckduck_viewdefs": pd.DataFrame(viewdefs, columns=["oid", "definition"]),
        }
        cur = c.cursor()
        try:
            cur.execute("BEGIN TRANSACTION")
            for table, frame in frames.items():
                cur.execute(f"DELETE FROM {s}.{table}")
                if len(frame):
                    view = f"_duckduck_fill_{table}"
                    cur.register(view, frame)
                    cols = ", ".join(frame.columns)
                    cur.execute(f"INSERT INTO {s}.{table} ({cols}) SELECT {cols} FROM {view}")
                    cur.unregister(view)
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
        finally:
            cur.close()
        self.oids = oids
        self.namespaces = namespaces
        #: (schema, table) as a client sees them, lowercased → the registered function
        self.functions = {(e["schema"].lower(), e["table"].lower()): e["function"] for e in entries}
        #: tables whose columns aren't known yet: oid → function, table name → functions
        self.unknown_oids = {e["oid"]: e["function"] for e in entries if not e["columns"]}
        self.unknown_names: Dict[str, List[str]] = {}
        for e in entries:
            if not e["columns"]:
                self.unknown_names.setdefault(e["table"].lower(), []).append(e["function"])

    @staticmethod
    def _class_row(oid: int, name: str, namespace: int, kind: str, natts: int) -> tuple:
        return (oid, name, namespace, 0, 0, _OWNER, 2 if kind == "r" else 0, oid, 0, 0, -1.0, 0, 0, False, False,
                "p", kind, natts, 0, False, False, False, False, False, False, True, "d", False, 0, 0, 0,
                None, None, None)

    def unknown_in(self, sql: str) -> List[str]:
        """The tables with unknown columns a catalog query names — by oid (a number) or by table name (a string)."""
        found: List[str] = []
        for number in re.findall(r"(?<![\w.])(\d{5,10})(?![\w.])", sql):
            fn = getattr(self, "unknown_oids", {}).get(int(number))
            if fn and fn not in found:
                found.append(fn)
        for text in re.findall(r"'((?:[^']|'')*)'", sql):
            for fn in getattr(self, "unknown_names", {}).get(text.replace("''", "'").lower(), []):
                if fn not in found:
                    found.append(fn)
        return found

    # -- rewriting a client's catalog query ------------------------------------------------------------

    def regclass(self, name: str) -> int:
        """``'pg_class'::regclass`` / ``'nvd.cves'::regclass`` → the oid (0 when unknown)."""
        text = name.strip().strip('"')
        if text.startswith("pg_catalog."):
            text = text[len("pg_catalog."):]
        if text in self.system_oids:
            return self.system_oids[text]
        schema, _, table = text.rpartition(".")
        return self.oids.get((schema or "public", table.strip('"')), 0)

    def regtype(self, name: str) -> int:
        text = name.strip().strip('"').lower().replace("pg_catalog.", "")
        for oid, (typname, _, _) in pg.TYPES.items():
            if text in (typname, pg.FORMAT_NAMES.get(oid, "")):
                return oid
        return {"int": pg.INT4, "integer[]": pg.ARRAY_OF[pg.INT4], "character varying": pg.VARCHAR}.get(text, 0)


_CATALOG_WORD = re.compile(r"\b(pg_catalog|information_schema|pg_[a-z_]+|current_user|session_user|regclass|regtype|"
                           r"regproc|regnamespace|regrole|"
                           + "|".join(sorted(MACRO_NAMES, key=len, reverse=True)) + r")\b", re.I)


def is_catalog_query(masked: str) -> bool:
    """Reads PostgreSQL's catalog or calls one of its functions (not a duckduck table)."""
    return bool(_CATALOG_WORD.search(masked))


_CAST_LITERAL = re.compile(r"('(?:[^']|'')*')\s*::\s*(?:pg_catalog\s*\.\s*)?(regclass|regtype|regproc|regprocedure|"
                           r"regnamespace|regrole)\b", re.I)
_CAST_TYPE = re.compile(r"::\s*(?:pg_catalog\s*\.\s*)?(regclass|regtype|regproc|regprocedure|regnamespace|regrole|"
                        r"oid|xid|cid|tid|name|\"char\"|int2vector|oidvector|aclitem|pg_node_tree|regconfig|"
                        r"text\s*\[\s*\]|name\s*\[\s*\]|\"char\"\s*\[\s*\]|oid\s*\[\s*\]|int2\s*\[\s*\])", re.I)
_CAST_TO = {"regclass": "UINTEGER", "regtype": "UINTEGER", "regproc": "UINTEGER", "regprocedure": "UINTEGER",
            "regnamespace": "UINTEGER", "regrole": "UINTEGER", "oid": "UINTEGER", "xid": "UINTEGER", "cid": "UINTEGER",
            "tid": "VARCHAR", "name": "VARCHAR", '"char"': "VARCHAR", "int2vector": "VARCHAR", "oidvector": "VARCHAR",
            "aclitem": "VARCHAR", "pg_node_tree": "VARCHAR", "regconfig": "VARCHAR"}


def rewrite(query: str, catalog: PgCatalog, user: str, masked_of: Any) -> str:
    """A client's catalog query, made to read ``duckduck_pg`` and to run on DuckDB."""
    q = query
    # quoted system names: "pg_catalog"."pg_class" → pg_catalog.pg_class
    q = re.sub(r'"(pg_catalog|information_schema|pg_[a-z_]+)"', r"\1", q)

    def literal_cast(m: re.Match) -> str:
        text, kind = m.group(1)[1:-1].replace("''", "'"), m.group(2).lower()
        if kind == "regclass":
            return str(catalog.regclass(text))
        if kind == "regtype":
            return str(catalog.regtype(text))
        if kind == "regnamespace":
            return str(catalog.namespaces.get(text.strip('"'), 0))
        if kind == "regrole":
            return str(_OWNER)
        return str(catalog.procs.get(re.sub(r"^pg_catalog\.", "", text.strip('"')), 0))  # regproc: pg_proc's oid

    q = _sub_outside(q, masked_of, _CAST_LITERAL, literal_cast)
    q = _sub_outside(q, masked_of, _CAST_TYPE, lambda m: "::" + (
        "VARCHAR[]" if "[" in m.group(1) and not m.group(1).lower().startswith(("oid", "int2")) else
        "UINTEGER[]" if m.group(1).lower().startswith("oid") and "[" in m.group(1) else
        "SMALLINT[]" if "[" in m.group(1) else _CAST_TO[m.group(1).lower()]))
    q = _sub_outside(q, masked_of, re.compile(r"(\bAS\s+)(?:pg_catalog\s*\.\s*)?(regclass|regtype|regproc|regprocedure|"
                                              r"regnamespace|regrole|oid|xid|name|int2vector|oidvector|pg_node_tree)"
                                              r"(?=\s*\))", re.I),
                     lambda m: m.group(1) + _CAST_TO[m.group(2).lower()])
    q = _sub_outside(q, masked_of, re.compile(r"(?<![\w.])json_build_object\s*\(", re.I), lambda m: "json_object(")
    q = _sub_outside(q, masked_of, re.compile(r"\bOPERATOR\s*\(\s*pg_catalog\s*\.\s*([^)\s]+)\s*\)", re.I),
                     lambda m: m.group(1))
    q = _sub_outside(q, masked_of, re.compile(r"\s+COLLATE\s+(?:pg_catalog\s*\.\s*)?(\"?default\"?|\"C\"|\"POSIX\")",
                                              re.I), lambda m: "")

    def qualified(m: re.Match) -> str:
        schema, name = m.group(1).lower(), m.group(2)
        low = name.lower()
        if schema == "information_schema":
            return f"{INFO_SCHEMA}.{name}" if low in INFO_VIEWS else m.group(0)
        if low in OBJECT_NAMES or low in MACRO_NAMES:
            return f"{SCHEMA}.{name}"
        return name  # a built-in function DuckDB has too: lower(), array_to_string()…

    q = _sub_outside(q, masked_of, re.compile(r"\b(pg_catalog|information_schema)\s*\.\s*(\w+)", re.I), qualified)
    q = _sub_outside(q, masked_of, re.compile(r"(?<![\w.\"$])(" + "|".join(sorted(MACRO_NAMES, key=len, reverse=True))
                                              + r")\s*\(", re.I), lambda m: f"{SCHEMA}.{m.group(1)}(")
    q = _sub_outside(q, masked_of, re.compile(r"(\b(?:FROM|JOIN)\s+|,\s*)(" + "|".join(
        sorted(OBJECT_NAMES, key=len, reverse=True)) + r")\b(?!\s*\.)", re.I),
        lambda m: f"{m.group(1)}{SCHEMA}.{m.group(2)}")
    q = _sub_outside(q, masked_of, re.compile(r"(?<![\w.\"])(current_user|session_user|current_role|user)\b"
                                              r"(?!\s*[(.])", re.I),
                     lambda m: f"{SCHEMA}.{'session_user_' if m.group(1).lower() == 'session_user' else 'current_user_'}()"
                     if _is_user_keyword(q, m) else m.group(0))
    q = _sub_outside(q, masked_of, re.compile(r"(?<![\w.\"])current_schema\b(?!\s*\()", re.I),
                     lambda m: f"{SCHEMA}.current_schema()")
    return q


def _is_user_keyword(query: str, m: re.Match) -> bool:
    """``user`` alone is PostgreSQL's current_user only where an expression starts (not a column called user)."""
    if m.group(1).lower() != "user":
        return True
    before = query[:m.start()].rstrip()
    return bool(re.search(r"(\bSELECT|,|=|\(|\bTHEN|\bELSE|\bWHEN)$", before, re.I)) and \
        not re.match(r"\s*(\w|\.)", query[m.end():m.end() + 2] or " ")


_FUNCTION_COLUMN = re.compile(r'^(?:' + SCHEMA + r'\.)?"?(\w+)"?\(.*\)$', re.S)
_COLUMN_NAMES = {"current_user_": "current_user", "session_user_": "session_user", "count_star": "count"}


def column_name(name: str) -> str:
    """A result column named after a rewritten call → the name PostgreSQL gives it (``version``, ``current_user``)."""
    m = _FUNCTION_COLUMN.match(name)
    if m is None:
        return name
    return _COLUMN_NAMES.get(m.group(1), m.group(1))


def _sub_outside(query: str, masked_of: Any, pattern: re.Pattern, repl: Any) -> str:
    """``pattern.sub`` on ``query``, matching where strings and comments are blanked (same length), so a name
    inside a string or a comment is never rewritten — while a pattern that starts with a literal still sees it."""
    hidden = masked_of(query)
    out, last = [], 0
    for m in pattern.finditer(hidden):
        real = pattern.match(query, m.start())
        if real is None or real.end() != m.end():
            continue
        out.append(query[last:m.start()])
        out.append(repl(real))
        last = m.end()
    out.append(query[last:])
    return "".join(out)

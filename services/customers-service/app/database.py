import time
import urllib.parse
import pymysql
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from app.config import settings

if settings.USE_IAM_AUTH:
    import google.auth
    from google.auth.transport.requests import Request

    class CloudSQLIAMAuthPlugin:
        def __init__(self, connection):
            self.connection = connection

        def authenticate(self, auth_packet):
            pwd = self.connection.password
            if isinstance(pwd, str):
                pwd = pwd.encode("utf-8")
            data = pwd + b"\0"
            self.connection.write_packet(data)
            pkt = self.connection._read_packet()
            pkt.check_error()
            return pkt

    def _connect_with_retry(**kwargs):
        # The first connections through a freshly programmed PSC / VPC-egress
        # path can exceed the connect timeout on a brand-new deploy (seen
        # live: (2003, "Can't connect to MySQL server ... (timed out)") on the
        # first DB-touching request, working seconds later). Retry the CONNECT
        # only — auth failures (1045) raise immediately with the fix.
        last = None
        for attempt in range(3):
            try:
                return pymysql.connect(**kwargs)
            except pymysql.err.OperationalError as e:
                if 1045 in e.args:
                    print(f"\n[CRITICAL] MySQL Access Denied (1045) for IAM user '{settings.DB_USER}'.")
                    print("Verify that the Cloud Run service account has been granted"
                          " 'roles/cloudsql.instanceUser' in the backend (database) project!")
                    raise
                last = e
                if attempt < 2:
                    print(f"[WARN] MySQL connect failed ({e.args[0] if e.args else '?'}),"
                          f" retry {attempt + 1}/2 in 2s ...")
                    time.sleep(2)
        raise last

    def get_pymysql_connection():
        credentials, project = google.auth.default(
            scopes=["https://www.googleapis.com/auth/sqlservice.login"]
        )
        credentials.refresh(Request())

        auth_map = {
            "authentication_cloudsql_iam": CloudSQLIAMAuthPlugin,
            "mysql_clear_password": CloudSQLIAMAuthPlugin,
            "cleartext": CloudSQLIAMAuthPlugin,
            "caching_sha2_password": CloudSQLIAMAuthPlugin,
        }

        if settings.CLOUD_SQL_CONNECTION_NAME and not settings.DB_HOST_IS_PSC:
            return _connect_with_retry(
                user=settings.DB_USER,
                password=credentials.token,
                unix_socket=f"/cloudsql/{settings.CLOUD_SQL_CONNECTION_NAME}",
                database=settings.DB_NAME,
                charset="utf8mb4",
                auth_plugin_map=auth_map,
                connect_timeout=10
            )
        else:
            # Connect via TCP (PSC Endpoint IP or Private IP) with mandatory SSL
            return _connect_with_retry(
                user=settings.DB_USER,
                password=credentials.token,
                host=settings.DB_HOST,
                port=settings.DB_PORT,
                database=settings.DB_NAME,
                charset="utf8mb4",
                ssl={"ssl_mode": "REQUIRED"},
                auth_plugin_map=auth_map,
                connect_timeout=10
            )

    engine = create_engine("mysql+pymysql://", creator=get_pymysql_connection, pool_pre_ping=True)
else:
    # Standard password authentication
    if settings.CLOUD_SQL_CONNECTION_NAME and not settings.DB_HOST_IS_PSC:
        query_params = urllib.parse.urlencode({"unix_socket": f"/cloudsql/{settings.CLOUD_SQL_CONNECTION_NAME}"})
        SQLALCHEMY_DATABASE_URL = f"mysql+pymysql://{settings.DB_USER}:{settings.DB_PASSWORD}@/{settings.DB_NAME}?{query_params}"
    else:
        SQLALCHEMY_DATABASE_URL = f"mysql+pymysql://{settings.DB_USER}:{settings.DB_PASSWORD}@{settings.DB_HOST}:{settings.DB_PORT}/{settings.DB_NAME}"

    engine = create_engine(SQLALCHEMY_DATABASE_URL, pool_pre_ping=True, connect_args={"connect_timeout": 10})

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

# Load database URL from environment variable
# SQLALCHEMY_DATABASE_URL = "postgresql://postgres:12345@localhost/editube"
SQLALCHEMY_DATABASE_URL = "postgresql://ditally-db_owner:uJfZYwcIz6d5@ep-royal-wood-a1on9e6p.ap-southeast-1.aws.neon.tech/editube_db?sslmode=require"


engine = create_engine(SQLALCHEMY_DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
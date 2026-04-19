import os
import sys

# Ensure app path is available
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy.orm import Session
from app.db.database import SessionLocal
from app.db.models import ForumCategory

def main():
    db: Session = SessionLocal()
    try:
        # Delete existing categories to ensure we only have the required ones
        db.query(ForumCategory).delete()
        
        # Add the ones the user explicitly requested
        categories_to_seed = [
            ForumCategory(name="Feedback", slug="feedback", color="#0ea5e9", description="Share your feedback and feature requests"),
            ForumCategory(name="Bug Report", slug="bug-report", color="#f43f5e", description="Report issues and crashes"),
            ForumCategory(name="Announcements", slug="announcements", color="#ef4444", description="Official news and announcements"),
            ForumCategory(name="Roadmap", slug="roadmap", color="#8b5cf6", description="Planned features and upcoming drops")
        ]
        
        db.add_all(categories_to_seed)
        db.commit()
        print("Successfully seeded community categories!")
    except Exception as e:
        print("Error seeding categories:", e)
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    main()

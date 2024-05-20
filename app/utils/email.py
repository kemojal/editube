# app/utils/email.py

import smtplib
from email.mime.text import MIMEText
from sqlalchemy.orm import Session
from app.db.models import User, Project
from app.db.database import get_db

def get_invitor_name(db: Session, creator_id: int):
    creator = db.query(User).filter(User.id == creator_id).first()
    return creator.name if creator else "Unknown User"




def send_invitation_email(email, project_id):
    db = next(get_db())
    project = db.query(Project).filter(Project.id == project_id).first()
    invitor_name = get_invitor_name(db, project.creator_id)
    
    sender_email = "kemo3855@gmail.com"  # Your email address
    receiver_email = email
    message = """
    Subject: Invitation to collaborate on a project
    
    Dear user,
    
    You have been invited by {} to collaborate on a project! Click the link below to register and start collaborating.
    
    Project Name: {}
    Register link: https://yourplatform.com/register
    
    Best regards,
    Your Team
    """.format(invitor_name, project.name)

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(sender_email, "bmhv cwln qigw vzhc")  # Use app-specific password or environment variables
        server.sendmail(sender_email, receiver_email, message)

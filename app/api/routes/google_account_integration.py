# from fastapi import APIRouter, Depends, HTTPException
# from sqlalchemy.orm import Session
# from app.db.database import get_db
# from app.api.models.users import GoogleAccount, User
# from app.utils.google_auth import google_oauth
# from app.utils.security import create_access_token, authenticate_user

# router = APIRouter()

# @router.post("/api/users/{user_id}/google-accounts")
# def link_google_account(user_id: int, code: str, db: Session = Depends(get_db)):
#     user = db.query(User).filter(User.id == user_id).first()
#     if not user:
#         raise HTTPException(status_code=404, detail="User not found")

#     try:
#         google_user_info, credentials = google_oauth.authenticate(code)
#     except Exception as e:
#         raise HTTPException(status_code=400, detail=str(e))

#     existing_account = db.query(GoogleAccount).filter(
#         GoogleAccount.userId == user_id,
#         GoogleAccount.googleId == google_user_info.get("id")
#     ).first()

#     if existing_account:
#         raise HTTPException(status_code=400, detail="Google account already linked")

#     google_account = GoogleAccount(
#         userId=user_id,
#         googleId=google_user_info.get("id"),
#         accessToken=credentials.token,
#         refreshToken=credentials.refresh_token
#     )
#     db.add(google_account)
#     db.commit()
#     db.refresh(google_account)

#     return google_account

# @router.delete("/api/users/{user_id}/google-accounts/{google_account_id}", status_code=204)
# def unlink_google_account(user_id: int, google_account_id: int, db: Session = Depends(get_db)):
#     google_account = db.query(GoogleAccount).filter(
#         GoogleAccount.id == google_account_id,
#         GoogleAccount.userId == user_id
#     ).first()

#     if not google_account:
#         raise HTTPException(status_code=404, detail="Google account not found")

#     db.delete(google_account)
#     db.commit()

#     return {"message": "Google account unlinked successfully"}

# @router.post("/api/auth/google")
# def google_login(code: str, db: Session = Depends(get_db)):
#     try:
#         google_user_info, credentials = google_oauth.authenticate(code)
#     except Exception as e:
#         raise HTTPException(status_code=400, detail=str(e))

#     google_account = db.query(GoogleAccount).filter(
#         GoogleAccount.googleId == google_user_info.get("id")
#     ).first()

#     if not google_account:
#         raise HTTPException(status_code=404, detail="User not found")

#     user = db.query(User).filter(User.id == google_account.userId).first()
#     access_token = create_access_token(user.id, user.role)

#     return {"access_token": access_token}
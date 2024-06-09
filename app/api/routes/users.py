from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from ..models.users import User as UserSchema, UserCreate, UserUpdate, UserRegisterSchema, UserLoginSchema
from ...db.database import get_db
from app.db.models import User
from ...utils.security import get_password_hash, verify_password, get_current_user, create_access_token, create_refresh_token, verify_refresh_token

router = APIRouter(
    prefix="/users",
    tags=["Users"],
)

# @router.post("/register")
# def register_user(user_data: UserRegisterSchema, db: Session = Depends(get_db)):
#     # Implement user registration logic here

#     return [{"username": "Rick"}, {"username": "Morty"}]

#     pass

# @router.post("/login")
# def login_user(user_data: UserLoginSchema, db: Session = Depends(get_db)):
#     # Implement user login logic here
#     pass

# # Add other user-related routes as needed
# @router.post("/register", response_model=UserRegisterSchema)
@router.post("/register")
def register_user(user: UserCreate, db: Session = Depends(get_db)):
    existing_user = db.query(User).filter(User.email == user.email).first()
    if existing_user:
        raise HTTPException(status_code=400, detail="Email already registered")
    
    hashed_password = get_password_hash(user.password)
    db_user = User(email=user.email, hashed_password=hashed_password, name=user.name, role=user.role)
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user

@router.post("/login")
def login_user(user_credentials: UserLoginSchema, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == user_credentials.email).first()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    
    if not verify_password(user_credentials.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    
    # Generate and return an access token
    access_token = create_access_token(data={"user_id": user.id})
    # Generate refresh token
    refresh_token = create_refresh_token(data={"user_id": user.id})

    return {"access_token": access_token, "token_type": "bearer", "refresh_token": refresh_token}


# Implement a token refresh endpoint
@router.post("/refresh-token")
def refresh_access_token(refresh_token: str, db: Session = Depends(get_db)):
    # Verify the refresh token
    user_id = verify_refresh_token(refresh_token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    # Generate a new access token
    access_token = create_access_token(data={"user_id": user_id})

    return {"access_token": access_token, "token_type": "bearer"}


# @router.get("/{user_id}", response_model=UserSchema)
@router.get("/{user_id}")
def get_user(user_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    userQuery = user = db.query(User)
    print("user query  = ", userQuery)
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user

@router.put("/{user_id}")
def update_user(user_id: int, user: UserUpdate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    print("current user = ", current_user.id)
    if current_user.id != user_id:
        raise HTTPException(status_code=403, detail="Not authorized to update this user")
    
    db_user = db.query(User).filter(User.id == user_id).first()
    if not db_user:
        raise HTTPException(status_code=404, detail="User not found")
    
    update_data = user.dict(exclude_unset=True)
    if update_data.get("password"):
        hashed_password = get_password_hash(update_data["password"])
        update_data["hashed_password"] = hashed_password
        del update_data["password"]
    
    for key, value in update_data.items():
        setattr(db_user, key, value)
    
    db.commit()
    db.refresh(db_user)
    return db_user
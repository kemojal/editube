from fastapi import APIRouter, Depends, HTTPException, File, UploadFile
from sqlalchemy.orm import Session
from pydantic import BaseModel

from ..models.users import User as UserSchema, UserCreate, UserUpdate, UserRegisterSchema, UserLoginSchema, OnboardingProfileUpdate, OnboardingWorkflowUpdate, OnboardingPlanUpdate
from ...db.database import get_db
from app.db.models import User
from app.api.models.users import UserResponse
from ...utils.security import get_password_hash, verify_password, get_current_user, create_access_token, create_refresh_token, verify_refresh_token
from app.utils.cloudinary import upload_file_to_cloudinary

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

    access_token = create_access_token(data={"user_id": db_user.id, "onboarding_completed": False})
    refresh_token = create_refresh_token(data={"user_id": db_user.id})
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "refresh_token": refresh_token,
        "onboarding_completed": False,
    }

@router.post("/login")
def login_user(user_credentials: UserLoginSchema, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == user_credentials.email).first()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    
    if not user.hashed_password:
        raise HTTPException(
            status_code=401,
            detail="This account uses Google sign-in. Continue with Google.",
        )

    if not verify_password(user_credentials.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    
    access_token = create_access_token(data={"user_id": user.id, "onboarding_completed": user.onboarding_completed})
    refresh_token = create_refresh_token(data={"user_id": user.id})
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "refresh_token": refresh_token,
        "onboarding_completed": user.onboarding_completed,
    }


class RefreshTokenBody(BaseModel):
    refresh_token: str


# Implement a token refresh endpoint
@router.post("/refresh-token")
def refresh_access_token(body: RefreshTokenBody, db: Session = Depends(get_db)):
    payload = verify_refresh_token(body.refresh_token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    user_id = payload.get("user_id")
    if user_id is None:
        raise HTTPException(status_code=401, detail="Invalid refresh token")
    user = db.query(User).filter(User.id == int(user_id)).first()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    access_token = create_access_token(
        data={"user_id": user.id, "onboarding_completed": user.onboarding_completed}
    )
    return {"access_token": access_token, "token_type": "bearer"}


@router.post("/sync-access-token")
def sync_access_token(current_user: User = Depends(get_current_user)):
    """Mint a new access token with current onboarding flags (e.g. after Stripe checkout)."""
    access_token = create_access_token(
        data={
            "user_id": current_user.id,
            "onboarding_completed": current_user.onboarding_completed,
        }
    )
    return {"access_token": access_token, "token_type": "bearer"}


# ── Onboarding Endpoints (must be before /{user_id} to avoid route conflict) ──

@router.get("/me", response_model=UserResponse)
def get_current_user_profile(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return current_user


@router.put("/onboarding/profile", response_model=UserResponse)
def onboarding_update_profile(
    data: OnboardingProfileUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    current_user.full_name = data.full_name
    if data.phone is not None:
        current_user.phone = data.phone
    if data.avatar_url is not None:
        current_user.avatar_url = data.avatar_url
    db.commit()
    db.refresh(current_user)
    return current_user


@router.post("/onboarding/avatar")
def onboarding_upload_avatar(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    file_url = upload_file_to_cloudinary(file)
    current_user.avatar_url = file_url
    db.commit()
    db.refresh(current_user)
    return {"avatar_url": file_url}


@router.put("/onboarding/workflow", response_model=UserResponse)
def onboarding_update_workflow(
    data: OnboardingWorkflowUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if data.workflow_type not in ("agency", "freelancer", "internal"):
        raise HTTPException(status_code=400, detail="Invalid workflow type")
    current_user.workflow_type = data.workflow_type
    db.commit()
    db.refresh(current_user)
    return current_user


@router.put("/onboarding/plan", response_model=UserResponse)
def onboarding_update_plan(
    data: OnboardingPlanUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Persist selected plan before Checkout. Onboarding completes after Stripe webhook."""
    if data.plan not in ("basic", "pro", "elite"):
        raise HTTPException(status_code=400, detail="Invalid plan")
    current_user.plan = data.plan
    db.commit()
    db.refresh(current_user)
    return current_user


# ── User CRUD (parameterized routes last) ──

@router.get("/{user_id}", response_model=UserResponse)
def get_user(user_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to access this user")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.put("/{user_id}")
def update_user(user_id: int, user: UserUpdate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
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
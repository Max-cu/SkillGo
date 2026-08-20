from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import current_user
from ..models import Role, User
from ..schemas import LoginRequest, RegistrationResponse, TokenResponse, UserCreate, UserRead
from ..security import create_access_token, hash_password, verify_password
from ..services import add_audit


router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=RegistrationResponse, status_code=status.HTTP_201_CREATED)
def register(payload: UserCreate, db: Session = Depends(get_db)) -> RegistrationResponse:
    email = payload.email.lower()
    if db.scalar(select(User).where(func.lower(User.email) == email)):
        raise HTTPException(status_code=409, detail="Email is already registered")
    requests_admin = payload.identity == "admin"
    user = User(
        email=email,
        display_name=payload.display_name.strip(),
        password_hash=hash_password(payload.password),
        role=Role.ADMIN if requests_admin else Role.USER,
        is_active=not requests_admin,
    )
    db.add(user)
    db.flush()
    add_audit(
        db,
        actor=user,
        action="auth.register.admin_request" if requests_admin else "auth.register.member",
        resource_type="user",
        resource_id=user.id,
    )
    db.commit()
    if requests_admin:
        return RegistrationResponse(
            status="pending_approval",
            message="管理员申请已提交，请等待超级管理员审核。",
            user=UserRead.model_validate(user),
        )
    return RegistrationResponse(
        status="active",
        message="成员账号已创建。",
        access_token=create_access_token(user.id),
        user=UserRead.model_validate(user),
    )


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    user = db.scalar(select(User).where(func.lower(User.email) == payload.email.lower()))
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Email or password is incorrect")
    if not user.is_active:
        if user.role == Role.ADMIN:
            raise HTTPException(status_code=403, detail="管理员申请正在等待超级管理员审核")
        raise HTTPException(status_code=403, detail="Account is disabled")
    add_audit(
        db,
        actor=user,
        action="auth.login",
        resource_type="user",
        resource_id=user.id,
    )
    db.commit()
    return TokenResponse(access_token=create_access_token(user.id), user=UserRead.model_validate(user))


@router.get("/me", response_model=UserRead)
def me(user: User = Depends(current_user)) -> User:
    return user

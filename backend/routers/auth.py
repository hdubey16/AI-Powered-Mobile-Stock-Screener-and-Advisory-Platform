from fastapi import APIRouter, HTTPException, status, Header, Depends
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, EmailStr
from datetime import datetime, timedelta
from typing import Optional
from dependencies import (
    verify_password, get_password_hash, create_access_token, 
    verify_token, get_current_user, ACCESS_TOKEN_EXPIRE_MINUTES, _storage
)
import dependencies as deps

router = APIRouter(prefix="/api/auth", tags=["auth"])

class UserSignup(BaseModel):
    name: str
    email: EmailStr
    password: str

class UserLogin(BaseModel):
    email: EmailStr
    password: str

class Token(BaseModel):
    access_token: str
    token_type: str
    user: dict

@router.post("/signup", response_model=Token)
async def signup(user: UserSignup):
    try:
        print(f"[SIGNUP] Attempting signup for email: {user.email}, name: {user.name}")
        
        # Validate input
        if not user.name or not user.name.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Name is required"
            )
        if not user.email or not user.email.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Email is required"
            )
        if not user.password or len(user.password) < 6:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Password must be at least 6 characters long"
            )

        # Check if user already exists
        existing_user = await _storage.get_user_by_email(user.email.strip().lower())
        if existing_user:
            print(f"[SIGNUP] Email already registered: {user.email}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered"
            )
        
        # Hash password and create user
        hashed_password = await run_in_threadpool(get_password_hash, user.password)
        user_id = await _storage.create_user(
            email=user.email.strip().lower(),
            username=user.email.split('@')[0],  # Use email prefix as username
            password_hash=hashed_password,
            full_name=user.name.strip(),
            is_admin=False
        )
        
        if not user_id:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create user"
            )
        
        print(f"[SIGNUP] Successfully created user: {user.email}, ID: {user_id}")
        
        # Create access token
        access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        access_token = create_access_token(
            data={"user_id": str(user_id), "email": user.email.strip().lower()},
            expires_delta=access_token_expires
        )
        
        return {
            "access_token": access_token,
            "token_type": "bearer",
            "user": {
                "id": str(user_id),
                "name": user.name.strip(),
                "email": user.email.strip().lower(),
                "role": "user"
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        error_type = type(e).__name__
        error_msg = str(e)
        print(f"[SIGNUP] Error: {error_type}: {error_msg}")
        
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An error occurred during signup: {str(e)}"
        )

@router.post("/login", response_model=Token)
async def login(user: UserLogin):
    try:
        print(f"[LOGIN] Attempting login for email: {user.email}")
        
        # Validate input
        if not user.email or not user.email.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Email is required"
            )
        if not user.password:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Password is required"
            )

        # Find user by email
        db_user = await _storage.get_user_by_email(user.email.strip().lower())
        if not db_user:
            print(f"[LOGIN] User not found: {user.email}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect email or password"
            )
        
        print(f"[LOGIN] User found: {db_user['email']}, verifying password...")
        
        # Verify password
        is_valid_password = await run_in_threadpool(verify_password, user.password, db_user.get("password_hash", ""))
        if not is_valid_password:
            print(f"[LOGIN] Password verification failed for: {user.email}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect email or password"
            )
        
        # Update last login
        await _storage.update_user(db_user["id"], {"last_login": datetime.utcnow()})
        
        print(f"[LOGIN] Login successful for: {user.email}")
        
        # Create access token
        access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        access_token = create_access_token(
            data={"user_id": str(db_user["id"]), "email": db_user["email"]},
            expires_delta=access_token_expires
        )
        
        return {
            "access_token": access_token,
            "token_type": "bearer",
            "user": {
                "id": str(db_user["id"]),
                "name": db_user.get("full_name", db_user.get("username", "")),
                "email": db_user["email"],
                "role": "admin" if db_user.get("is_admin") else "user"
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        error_type = type(e).__name__
        error_msg = str(e)
        print(f"[LOGIN] Error: {error_type}: {error_msg}")
        
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An error occurred during login: {str(e)}"
        )

@router.get("/me")
async def me(current_user: dict = Depends(get_current_user)):
    return current_user

class UserUpdate(BaseModel):
    name: str

@router.put("/profile")
async def update_profile(user_update: UserUpdate, current_user: dict = Depends(get_current_user)):
    """Update user profile"""
    try:
        if not user_update.name or not user_update.name.strip():
             raise HTTPException(status_code=400, detail="Name cannot be empty")
             
        user_id = int(current_user["id"])
        
        # Update in TimescaleDB
        success = await _storage.update_user(
            user_id,
            {"full_name": user_update.name.strip()}
        )
        
        if not success:
            raise HTTPException(status_code=500, detail="Failed to update profile")
             
        return {"message": "Profile updated", "user": {**current_user, "name": user_update.name.strip()}}
        
    except Exception as e:
        print(f"[PROFILE UPDATE ERROR] {e}")
        raise HTTPException(status_code=500, detail=str(e))

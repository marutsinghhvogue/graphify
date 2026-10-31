"""user_service — FastAPI producer. Owns the /users/* path namespace."""
from fastapi import FastAPI

app = FastAPI()


@app.get("/users/{user_id}")
def get_user(user_id: str):
    return {"id": user_id}


@app.post("/users")
def create_user():
    return {}

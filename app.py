import json
import os
from typing import Optional

import psycopg2
import psycopg2.extras
import redis
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

PRIMARY_DSN = os.getenv(
    "PRIMARY_DSN",
    "host=haproxy port=5432 dbname=jus_de_pomdb user=jus_de_pom password=jus_de_pom_pwd",
)
REPLICA_DSN = os.getenv(
    "REPLICA_DSN",
    "host=db-replica port=5432 dbname=jus_de_pomdb user=jus_de_pom password=jus_de_pom_pwd",
)

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))

CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "60"))

# Timeout court pour éviter de bloquer l’API si Redis est down
r = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    decode_responses=True,
    socket_connect_timeout=0.3,
    socket_timeout=0.3,
)

app = FastAPI(title="Products API (Primary writes, Replica reads, Redis cache-aside)")


def get_primary_conn():
    return psycopg2.connect(PRIMARY_DSN, cursor_factory=psycopg2.extras.RealDictCursor)


def get_replica_conn():
    return psycopg2.connect(REPLICA_DSN, cursor_factory=psycopg2.extras.RealDictCursor)


class ProductUpdate(BaseModel):
    name: Optional[str] = None
    price_cents: Optional[int] = None


class ProductCreate(BaseModel):
    name: str
    price_cents: int


def cache_key(product_id: int) -> str:
    return f"product:{product_id}"


# --------- Helpers “best effort” (ne cassent pas l’API si Redis/replica tombent) ---------

def redis_get_json(key: str):
    try:
        val = r.get(key)
        return json.loads(val) if val else None
    except (redis.exceptions.RedisError, OSError, ConnectionError):
        return None


def redis_set_json(key: str, value: dict, ttl_seconds: int):
    try:
        r.setex(key, ttl_seconds, json.dumps(value, default=str))
    except (redis.exceptions.RedisError, OSError, ConnectionError):
        pass


def redis_del(key: str):
    try:
        r.delete(key)
    except (redis.exceptions.RedisError, OSError, ConnectionError):
        pass


def fetch_product_from_db(dsn: str, product_id: int):
    with psycopg2.connect(dsn, cursor_factory=psycopg2.extras.RealDictCursor) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, name, price_cents, updated_at FROM products WHERE id = %s",
                (product_id,),
            )
            return cur.fetchone()


@app.get("/products/{product_id}")
def get_product(product_id: int):
    key = cache_key(product_id)

    # 1) Redis (best-effort)
    cached = redis_get_json(key)
    if cached:
        return cached

    # 2) DB replica, sinon fallback primary (via HAProxy)
    row = None
    try:
        row = fetch_product_from_db(REPLICA_DSN, product_id)
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        # replica down -> fallback
        try:
            row = fetch_product_from_db(PRIMARY_DSN, product_id)
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            raise HTTPException(status_code=503, detail=f"Database unavailable: {e.__class__.__name__}")

    if not row:
        raise HTTPException(status_code=404, detail="Product not found")

    # 3) Mise en cache (best-effort)
    redis_set_json(key, row, CACHE_TTL_SECONDS)
    return row


@app.put("/products/{product_id}")
def update_product(product_id: int, payload: ProductUpdate):
    if payload.name is None and payload.price_cents is None:
        raise HTTPException(status_code=400, detail="No fields to update")

    set_parts = []
    values = []
    if payload.name is not None:
        set_parts.append("name = %s")
        values.append(payload.name)
    if payload.price_cents is not None:
        set_parts.append("price_cents = %s")
        values.append(payload.price_cents)

    set_parts.append("updated_at = now()")
    values.append(product_id)

    query = f"""
        UPDATE products
        SET {", ".join(set_parts)}
        WHERE id = %s
        RETURNING id, name, price_cents, updated_at
    """

    # Écriture sur primary (via HAProxy). Si primary down -> 503
    try:
        with get_primary_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(query, tuple(values))
                updated = cur.fetchone()
            conn.commit()
    except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
        raise HTTPException(status_code=503, detail=f"Primary database unavailable: {e.__class__.__name__}")

    if not updated:
        raise HTTPException(status_code=404, detail="Product not found")

    # Invalidation cache (best-effort)
    redis_del(cache_key(product_id))

    return updated


@app.post("/products", status_code=201)
def create_product(payload: ProductCreate):
    # Insertion sur primary (via HAProxy). Si primary down -> 503
    try:
        with get_primary_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO products (name, price_cents)
                    VALUES (%s, %s)
                    RETURNING id, name, price_cents, updated_at
                    """,
                    (payload.name, payload.price_cents),
                )
                created = cur.fetchone()
            conn.commit()
    except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
        raise HTTPException(status_code=503, detail=f"Primary database unavailable: {e.__class__.__name__}")

    # Pré-remplissage cache (best-effort)
    redis_set_json(cache_key(created["id"]), created, CACHE_TTL_SECONDS)

    return created

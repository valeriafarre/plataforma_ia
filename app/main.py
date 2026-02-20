from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
import pandas as pd
from pathlib import Path
from math import ceil
from fastapi.responses import StreamingResponse
import io


class PLItem(BaseModel):
    sku_stone: str
    quantity: int

class PackingListIn(BaseModel):
    items: list[PLItem]


app = FastAPI()

# 1) "Base de datos" temporal en memoria (luego será Supabase)
USERS = {
    # Admin interno: todo
    "valeria_admin": {
        "password": "1234",
        "role": "admin",
        "countries": ["ALL"],
        "clients": ["ALL"],
    },

    # Comercial Colombia: solo CO, pero puede ver 2 clientes específicos
    "juan_co": {
        "password": "1234",
        "role": "comercial",
        "countries": ["CO"],
        "clients": ["A", "B"],
    },

    # Servicio al cliente: puede ver CO y US, pero solo un cliente
    "soporte_1": {
        "password": "1234",
        "role": "servicio_cliente",
        "countries": ["CO", "US"],
        "clients": ["A"],
    },

    # Usuario del cliente (cliente A): solo su cuenta
    "cliente_a_user": {
        "password": "1234",
        "role": "cliente",
        "countries": ["CO"],
        "clients": ["A"],
    },
}


# 2) Tokens temporales (luego serán JWT)
TOKENS = {}  # token -> username

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

PRODUCTS_CSV = DATA_DIR / "products.csv"
PRICES_CSV = DATA_DIR / "prices.csv"
CLIENTS_CSV = DATA_DIR / "clients.csv"


# --------- Modelos de entrada ----------
class LoginIn(BaseModel):
    username: str
    password: str


# --------- Helpers de seguridad ----------
def require_token(x_token: str | None) -> dict:
    """
    Verifica que el usuario mandó un token válido en el header X-Token
    """
    if not x_token or x_token not in TOKENS:
        raise HTTPException(status_code=401, detail="Falta token o token inválido")

    username = TOKENS[x_token]
    user = USERS.get(username)
    if not user:
        raise HTTPException(status_code=401, detail="Usuario no existe")

    return {"username": username, **user}


def enforce_role_country(user: dict, resource_country: str):
    """
    Regla de país:
    - admin y financiero: todos los países
    - comercial / cliente_comercial / técnicos: solo sus países
    """
    role = user["role"]

    if role in {"admin", "financiero"}:
        return

    countries = [c.upper() for c in user.get("countries", [])]
    if "ALL" not in countries and resource_country.upper() not in countries:
        raise HTTPException(status_code=403, detail="No tienes acceso a este país")



ROLE_PERMISSIONS = {
    "products:read": {"admin", "comercial", "cliente_comercial"},
    "prices:read": {"admin", "comercial"},
    # (más adelante)
    "packing_list:create": {"admin", "comercial"},
    "tickets:create": {"admin", "tecnico_interno"},
    "tech:read_external": {"admin", "tecnico_interno", "tecnico_externo"},
    "finance:read": {"admin", "financiero"},
}

def require_permission(user: dict, permission: str):
    allowed_roles = ROLE_PERMISSIONS.get(permission)
    if not allowed_roles:
        raise HTTPException(status_code=500, detail=f"Permiso no configurado: {permission}")

    if user["role"] not in allowed_roles:
        raise HTTPException(status_code=403, detail="Tu rol no tiene permiso para esta acción")

def get_client_record(client_id: str) -> dict:
    """
    Busca el cliente en clients.csv y devuelve su registro.
    client_id = valor de query param ?client=...
    """
    if not CLIENTS_CSV.exists():
        raise HTTPException(status_code=500, detail=f"No existe el archivo: {CLIENTS_CSV}")

    df = pd.read_csv(CLIENTS_CSV).fillna("")
    if "cliente" not in df.columns:
        raise HTTPException(status_code=500, detail="clients.csv debe tener columna 'cliente'")

    df["cliente"] = df["cliente"].astype(str).str.upper().str.strip()

    client_up = client_id.upper().strip()
    row = df[df["cliente"] == client_up]

    if row.empty:
        raise HTTPException(status_code=404, detail=f"Cliente no existe en clients.csv: {client_up}")

    return row.iloc[0].to_dict()

# --------- Endpoints ----------
@app.get("/")
def home():
    return {"mensaje": "App funcionando 🚀", "next": "Ve a /docs"}

@app.post("/auth/login")
def login(data: LoginIn):
    user = USERS.get(data.username)
    if not user or user["password"] != data.password:
        raise HTTPException(status_code=401, detail="Usuario o clave incorrectos")

    # token súper simple solo para aprender (luego JWT)
    token = f"token-{data.username}"
    TOKENS[token] = data.username

    return {
    "token": token,
    "role": user["role"],
    "countries": user.get("countries", []),
    "clients": user.get("clients", []),
}



@app.get("/products")
def get_products(x_token: str | None = Header(default=None, alias="X-Token")):
    user = require_token(x_token)

    # Catálogo global: solo validamos rol (no país)
    require_permission(user, "products:read")


    if not PRODUCTS_CSV.exists():
        raise HTTPException(status_code=500, detail=f"No existe el archivo: {PRODUCTS_CSV}")

    df = pd.read_csv(PRODUCTS_CSV)
    # ✅ Convertir NaN (celdas vacías) a string vacío para que JSON no falle
    df = df.fillna("")

    return {
    "user": {
        "username": user["username"],
        "role": user["role"],
        "countries": user.get("countries", []),
        "clients": user.get("clients", []),
        },
        "count": int(len(df)),
        "products": df.to_dict(orient="records"),
    }




@app.get("/prices/{country_code}")
def get_prices(
    country_code: str,
    x_token: str | None = Header(default=None, alias="X-Token"),
    client: str | None = None,   # /prices/CO?client=CLIENTE_A
    port: str | None = None,     # /prices/CO?client=CLIENTE_A&port=Shenzhen
):
    user = require_token(x_token)

    # 1) Permiso por rol
    require_permission(user, "prices:read")

    # 2) Validación por país (según tu lógica)
    enforce_role_country(user, country_code.upper())

    if not PRICES_CSV.exists():
        raise HTTPException(status_code=500, detail=f"No existe el archivo: {PRICES_CSV}")

    df = pd.read_csv(PRICES_CSV).fillna("")

    # 3) Normalizar columnas clave
    df["country"] = df["country"].astype(str).str.upper().str.strip()
    df["client"] = df["client"].astype(str).str.upper().str.strip()
    df["port"] = df["port"].astype(str).str.strip()
    df["origin_country_distribution"] = df["origin_country_distribution"].astype(str).str.upper().str.strip()
    df["sku_stone"] = df["sku_stone"].astype(str).str.strip()

    # 4) Filtrar por país
    filtered = df[df["country"] == country_code.upper()]

    # 5) Filtrar por cliente (si viene)
    if client:
        client_up = client.upper().strip()

        allowed_clients = [c.upper() for c in user.get("clients", [])]
        if "ALL" not in allowed_clients and client_up not in allowed_clients:
            raise HTTPException(status_code=403, detail="No tienes acceso a este cliente")
        client_info = get_client_record(client_up)

        filtered = filtered[filtered["client"] == client_up]
        

    # 6) Filtrar por port (si viene)
    if port:
        port_norm = port.strip()
        filtered = filtered[filtered["port"] == port_norm]

    return {
        "country": country_code.upper(),
        "client": (client.upper().strip() if client else None),
        "port": (port.strip() if port else None),
        "count": int(len(filtered)),
        "prices": filtered.to_dict(orient="records"),
    }

@app.get("/catalog/{country_code}")
def get_catalog(
    country_code: str,
    x_token: str | None = Header(default=None, alias="X-Token"),
    client: str | None = None,
    port: str | None = None,
):
    user = require_token(x_token)

    # Permiso para ver catálogo
    require_permission(user, "products:read")

    # Validación por país (si aplica)
    enforce_role_country(user, country_code.upper())

    # Validación de cliente (si viene)
    if client:
        client_up = client.upper().strip()
        allowed_clients = [c.upper() for c in user.get("clients", [])]
        if "ALL" not in allowed_clients and client_up not in allowed_clients:
            raise HTTPException(status_code=403, detail="No tienes acceso a este cliente")
    else:
        raise HTTPException(status_code=422, detail="Falta query param obligatorio: client")

    if not PRODUCTS_CSV.exists():
        raise HTTPException(status_code=500, detail=f"No existe el archivo: {PRODUCTS_CSV}")
    if not PRICES_CSV.exists():
        raise HTTPException(status_code=500, detail=f"No existe el archivo: {PRICES_CSV}")

    # --- Leer products ---
    prod = pd.read_csv(PRODUCTS_CSV).fillna("")
    # Normaliza llave
    if "stone" not in prod.columns:
        raise HTTPException(status_code=500, detail="products.csv debe tener columna 'stone' como llave")
    prod["stone"] = prod["stone"].astype(str).str.strip()

    # --- Leer prices ---
    pr = pd.read_csv(PRICES_CSV).fillna("")
    pr["country"] = pr["country"].astype(str).str.upper().str.strip()
    pr["client"] = pr["client"].astype(str).str.upper().str.strip()
    pr["sku_stone"] = pr["sku_stone"].astype(str).str.strip()
    pr["port"] = pr["port"].astype(str).str.strip()

    # Filtrar prices por país + cliente (+port opcional)
    pr_f = pr[(pr["country"] == country_code.upper()) & (pr["client"] == client_up)]
    if port:
        port_norm = port.strip()
        pr_f = pr_f[pr_f["port"] == port_norm]

    # Quedarnos con una fila por sku_stone (si hay duplicados)
    # regla: si hay múltiples, tomamos la primera que quede luego del filtro
    pr_f = pr_f.drop_duplicates(subset=["sku_stone"], keep="first")

    # --- Merge ---
    merged = prod.merge(
        pr_f[["sku_stone", "price", "port", "origin_country_distribution"]],
        how="left",
        left_on="stone",
        right_on="sku_stone",
    )

    # Limpieza final
    merged = merged.drop(columns=["sku_stone"])
    merged = merged.fillna("")

    # Solo para UX: bandera si tiene precio o no
    merged["has_price"] = merged["price"].apply(lambda x: False if x == "" else True)

    return {
        "country": country_code.upper(),
        "client": client_up,
        "port": (port.strip() if port else None),
        "count": int(len(merged)),
        "priced_count": int(merged["has_price"].sum()),
        "products": merged.to_dict(orient="records"),
    }

@app.post("/packing-list/{country_code}")
def create_packing_list(
    country_code: str,
    data: PackingListIn,
    client: str,
    port: str,
    x_token: str | None = Header(default=None, alias="X-Token"),
):
    user = require_token(x_token)

    # permisos: quien puede generar PL (ajústalo si quieres)
    require_permission(user, "products:read")
    enforce_role_country(user, country_code.upper())

    # validar cliente contra usuario
    # 1) Normalizar cliente
    client_up = client.upper().strip()

    # 2) Validar que el cliente exista (404 si no existe)
    client_info = get_client_record(client_up)
    if not client_info:
        raise HTTPException(status_code=404, detail="Cliente no existe")

    # 3) Validar permisos del usuario (403 si no tiene acceso)
    allowed_clients = [c.upper() for c in user.get("clients", [])]
    if "ALL" not in allowed_clients and client_up not in allowed_clients:
        raise HTTPException(status_code=403, detail="No tienes acceso a este cliente")

    if not PRODUCTS_CSV.exists():
        raise HTTPException(status_code=500, detail=f"No existe el archivo: {PRODUCTS_CSV}")
    if not PRICES_CSV.exists():
        raise HTTPException(status_code=500, detail=f"No existe el archivo: {PRICES_CSV}")

    prod = pd.read_csv(PRODUCTS_CSV).fillna("")
    pr = pd.read_csv(PRICES_CSV).fillna("")

    # normalizar claves
    prod["stone"] = prod["stone"].astype(str).str.strip()

    pr["country"] = pr["country"].astype(str).str.upper().str.strip()
    pr["client"] = pr["client"].astype(str).str.upper().str.strip()
    pr["sku_stone"] = pr["sku_stone"].astype(str).str.strip()
    pr["port"] = pr["port"].astype(str).str.strip()

    # filtrar precios por pais+cliente+puerto (puerto único)
    pr_f = pr[
        (pr["country"] == country_code.upper())
        & (pr["client"] == client_up)
        & (pr["port"] == port.strip())
    ].drop_duplicates(subset=["sku_stone"], keep="first")

    # map rápido sku -> fila precio
    price_map = {row["sku_stone"]: row for _, row in pr_f.iterrows()}

    rows = []
    total_usd = 0.0
    total_cajas = 0
    total_peso_bruto = 0.0
    total_volumen = 0.0

    for item in data.items:
        sku = str(item.sku_stone).strip()
        qty = int(item.quantity)

        p_row = prod[prod["stone"] == sku]
        if p_row.empty:
            raise HTTPException(status_code=404, detail=f"SKU stone no existe en products.csv: {sku}")
        p = p_row.iloc[0].to_dict()

        if sku not in price_map:
            raise HTTPException(
                status_code=404,
                detail=f"No hay precio para SKU {sku} con country={country_code.upper()}, client={client_up}, port={port}",
            )
        prc = price_map[sku]

        unidades_caja = int(p.get("u_por_caja", 0) or 0)
        if unidades_caja <= 0:
            raise HTTPException(status_code=422, detail=f"u_por_caja inválido para SKU {sku}")

        cajas = ceil(qty / unidades_caja)

        peso_bruto_caja = float(p.get("peso_bruto_kg", 0) or 0)
        cbm_caja = float(p.get("CBM", 0) or 0)

        precio_unit = float(prc.get("price", 0) or 0)
        total_linea = qty * precio_unit

        peso_bruto_total = cajas * peso_bruto_caja
        vol_total = cajas * cbm_caja

        total_usd += total_linea
        total_cajas += cajas
        total_peso_bruto += peso_bruto_total
        total_volumen += vol_total

        rows.append({
            "stone": sku,
            "descripcion": p.get("descripcion", ""),
            "referencia": p.get("referencia", ""),
            "cantidad": qty,
            "precio_unit": precio_unit,
            "total_usd": total_linea,

            "unidades_dentro_caja": unidades_caja,
            "alto_cm": p.get("alto_cm", ""),
            "ancho_cm": p.get("ancho_cm", ""),
            "largo_cm": p.get("largo_cm", ""),
            "peso_neto_kg": p.get("peso_neto_kg", ""),
            "peso_bruto_kg": peso_bruto_caja,
            "volumen_m3": cbm_caja,

            "cajas_master_bultos": cajas,
            "peso_bruto_total_kg": peso_bruto_total,
            "volumen_total_m3": vol_total,

            "pais_origen": prc.get("origin_country_distribution", ""),
            "posicion_arancelaria": p.get("posicion_arancelaria", ""),
            "puerto": port.strip(),
        })

    return {
        "country": country_code.upper(),
        "client": client_up,
        "port": port.strip(),
        "client_info": client_info,
        "count": len(rows),
        "totals": {
            "total_usd": total_usd,
            "total_cajas_master": total_cajas,
            "total_peso_bruto_kg": total_peso_bruto,
            "total_volumen_m3": total_volumen,
        },
        "rows": rows,
    }

@app.post("/packing-list/{country_code}/excel")
def packing_list_excel(
    country_code: str,
    data: PackingListIn,
    client: str,
    port: str,
    x_token: str | None = Header(default=None, alias="X-Token"),
):
    # reutilizamos el endpoint que ya funciona
    result = create_packing_list(
        country_code=country_code,
        data=data,
        client=client,
        port=port,
        x_token=x_token,
    )

    df = pd.DataFrame(result["rows"])

    # Ordena columnas como tu PL
    columns_order = [
        "stone", "descripcion", "referencia", "cantidad",
        "precio_unit", "total_usd",
        "unidades_dentro_caja", "alto_cm", "ancho_cm", "largo_cm",
        "peso_neto_kg", "peso_bruto_kg", "volumen_m3",
        "cajas_master_bultos", "peso_bruto_total_kg", "volumen_total_m3",
        "pais_origen", "posicion_arancelaria", "puerto"
    ]
    df = df[columns_order]

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name="Packing List")

        # Totales al final
        totals_df = pd.DataFrame([result["totals"]])
        totals_df.to_excel(writer, index=False, sheet_name="Totales")

    output.seek(0)

    filename = f"Packing_List_{country_code}_{client}_{port}.xlsx"
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )

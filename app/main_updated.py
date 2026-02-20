from fastapi import FastAPI, HTTPException, Header, Depends
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime
import csv
import pandas as pd
from pathlib import Path
from math import ceil
from fastapi.responses import StreamingResponse
import io
from fastapi.responses import FileResponse

import os
try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = lambda: None  # type: ignore

try:
    from supabase import create_client, Client
except Exception:  # pragma: no cover
    create_client = None  # type: ignore
    Client = None  # type: ignore

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
# Prefer service role key on backend; fallback to anon if user only has that for local testing
SUPABASE_KEY = (
    os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    or os.getenv("SUPABASE_ANON_KEY", "").strip()
)

supabase = None
if create_client and SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

def supabase_enabled() -> bool:
    return supabase is not None


class PLItem(BaseModel):
    sku_stone: str
    quantity: int

class PackingListIn(BaseModel):
    warehouse_origin: Optional[str] = ""
    incoterm: Optional[str] = ""
    items: List[PLItem]


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

PACKING_LISTS_CSV = DATA_DIR / "packing_lists.csv"

PACKING_LISTS_FIELDS = [
 "pl_id","date","country","client","port","created_by",
 "quotation_number","warehouse_origin","incoterm",
 "items_count","total_units","excel_file",
 "idempotency_key"
]

OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True, parents=True)



# --------- Modelos de entrada ----------
class LoginIn(BaseModel):
    username: str
    password: str


# --------- Helpers de seguridad ----------

def get_pl_row_or_404(pl_id: str) -> dict:
    # Supabase primero
    if supabase_enabled():
        res = (
            supabase.table("packing_lists")
            .select("*")
            .eq("pl_id", pl_id)
            .limit(1)
            .execute()
        )
        if not res.data:
            raise HTTPException(status_code=404, detail="PL no existe")
        return res.data[0]

    # Fallback CSV
    if not PACKING_LISTS_CSV.exists():
        raise HTTPException(status_code=404, detail="PL no existe")

    df = pd.read_csv(PACKING_LISTS_CSV).fillna("")
    m = df[df["pl_id"].astype(str) == pl_id]
    if len(m) == 0:
        raise HTTPException(status_code=404, detail="PL no existe")

    return m.iloc[0].to_dict()

def ensure_packing_lists_csv():
    """Crea/normaliza data/packing_lists.csv para evitar que el append se pegue al header."""
    DATA_DIR.mkdir(exist_ok=True, parents=True)

    # Si no existe o está vacío -> crear con header
    if (not PACKING_LISTS_CSV.exists()) or PACKING_LISTS_CSV.stat().st_size == 0:
        with open(PACKING_LISTS_CSV, "w", encoding="utf-8", newline="") as f:
            import csv as _csv
            _csv.writer(f).writerow(PACKING_LISTS_FIELDS)
        return

    # Si existe pero NO termina en newline -> agregar newline
    with open(PACKING_LISTS_CSV, "rb") as f:
        last = f.read()[-1:]
    if last and last != b"\n":
        with open(PACKING_LISTS_CSV, "ab") as f:
            f.write(b"\n")

    # Si el header es distinto -> reescribir header y mantener filas
    with open(PACKING_LISTS_CSV, "r", encoding="utf-8", newline="") as f:
        first = f.readline().strip("\r\n")

    expected = ",".join(PACKING_LISTS_FIELDS)
    if first != expected:
        lines = PACKING_LISTS_CSV.read_text(encoding="utf-8").splitlines(True)
        rest = lines[1:] if len(lines) > 0 else []
        with open(PACKING_LISTS_CSV, "w", encoding="utf-8", newline="") as f:
            f.write(expected + "\n")
            f.writelines(rest)

def next_pl_id() -> str:
    today = datetime.now().strftime("%Y%m%d")

    # Supabase
    if supabase_enabled():
        prefix = f"PL-{today}-"
        res = (
            supabase.table("packing_lists")
            .select("pl_id")
            .like("pl_id", f"{prefix}%")
            .execute()
        )
        if not res.data:
            return f"PL-{today}-0001"

        # extraer consecutivo
        nums = []
        for r in res.data:
            pid = str(r.get("pl_id", ""))
            try:
                nums.append(int(pid.split("-")[-1]))
            except Exception:
                pass
        last_num = max(nums) if nums else 0
        return f"PL-{today}-{last_num+1:04d}"

    # Fallback CSV
    ensure_packing_lists_csv()
    df = pd.read_csv(PACKING_LISTS_CSV).fillna("")
    today_prefix = f"PL-{today}-"
    todays = df[df["pl_id"].astype(str).str.startswith(today_prefix)]
    if len(todays) == 0:
        return f"PL-{today}-0001"

    last_num = (
        todays["pl_id"]
        .astype(str)
        .str.split("-")
        .str[-1]
        .astype(int)
        .max()
    )
    return f"PL-{today}-{int(last_num)+1:04d}"




def next_quotation_number() -> str:
    """Consecutivo diario: Q-YYYYMMDD-0001"""
    today = datetime.now().strftime("%Y%m%d")
    prefix = f"Q-{today}-"

    if supabase_enabled():
        res = (
            supabase.table("packing_lists")
            .select("quotation_number")
            .like("quotation_number", f"{prefix}%")
            .execute()
        )
        if not res.data:
            return f"Q-{today}-0001"

        nums = []
        for r in res.data:
            qn = str(r.get("quotation_number", ""))
            try:
                nums.append(int(qn.split("-")[-1]))
            except Exception:
                pass
        last_num = max(nums) if nums else 0
        return f"Q-{today}-{last_num+1:04d}"

    # Fallback CSV
    if not PACKING_LISTS_CSV.exists():
        return f"Q-{today}-0001"

    df = pd.read_csv(PACKING_LISTS_CSV).fillna("")
    if "quotation_number" not in df.columns:
        return f"Q-{today}-0001"

    todays = df[df["quotation_number"].astype(str).str.startswith(prefix)]
    if len(todays) == 0:
        return f"Q-{today}-0001"

    last_num = (
        todays["quotation_number"]
        .astype(str)
        .str.split("-")
        .str[-1]
        .astype(int)
        .max()
    )
    return f"Q-{today}-{last_num+1:04d}"


def append_packing_list_row(row: dict):
    """Append a packing list row to CSV (fallback). En Supabase, se guarda vía insert."""
    if supabase_enabled():
        # En modo Supabase, no escribimos CSV
        return

    ensure_packing_lists_csv()
    with open(PACKING_LISTS_CSV, "a", newline="", encoding="utf-8") as f:
        import csv as _csv
        writer = _csv.DictWriter(f, fieldnames=PACKING_LISTS_FIELDS)
        safe_row = {k: row.get(k, "") for k in PACKING_LISTS_FIELDS}
        writer.writerow(safe_row)



def build_packing_list_excel(result: dict, output_path: Path):
    """Genera y guarda el Excel del Packing List / Quotation con estilo corporativo."""
    import pandas as pd

    # ===== Data =====
    df = pd.DataFrame(result.get("rows", []))

    # OJO: tu data usa "stone" (no sku_stone)
    columns_order = [
        "stone", "descripcion", "referencia", "cantidad",
        "precio_unit", "total_usd",
        "unidades_dentro_caja", "alto_cm", "ancho_cm", "largo_cm",
        "peso_neto_kg", "peso_bruto_kg", "volumen_m3",
        "cajas_master_bultos", "peso_bruto_total_kg", "volumen_total_m3",
        "pais_origen", "posicion_arancelaria", "puerto"
    ]
    columns_order = [c for c in columns_order if c in df.columns]
    df = df[columns_order]

    # ===== Branding =====
    PRIMARY = "#000000"
    SECONDARY = "#93D500"  # verde
    FONT = "Libre Franklin"

    # Logo (assets/logo.png)
    try:
        logo_path = str((BASE_DIR / "assets" / "logo.png").resolve())
    except Exception:
        logo_path = "assets/logo.png"

    # ===== Helpers =====
    client_info = result.get("client_info", {}) or {}
    client_id = str(result.get("client", "") or "")
    country = str(result.get("country", "") or "").upper().strip()

    # Tax: US -> 7%, else 0%
    tax_rate = 0.07 if country in ("US", "USA", "UNITED STATES") else 0.0
    subtotal = float(result.get("totals", {}).get("total_usd", 0) or 0)
    sales_tax = round(subtotal * tax_rate, 2)
    grand_total = round(subtotal + sales_tax, 2)

    # Salesperson (username por ahora)
    salesperson = str(result.get("created_by", "") or "")

    # ===== Create Excel =====
    with pd.ExcelWriter(output_path, engine="xlsxwriter") as writer:
        wb = writer.book
        ws = wb.add_worksheet("Packing List")
        writer.sheets["Packing List"] = ws

        # ===== Formats =====
        fmt_title = wb.add_format({
            "font_name": FONT, "font_size": 16, "bold": True,
            "font_color": PRIMARY, "align": "left", "valign": "vcenter"
        })
        fmt_addr = wb.add_format({
            "font_name": FONT, "font_size": 9,
            "font_color": PRIMARY, "align": "left", "valign": "vcenter"
        })
        fmt_label = wb.add_format({
            "font_name": FONT, "font_size": 10, "bold": True,
            "font_color": PRIMARY, "align": "left", "valign": "vcenter"
        })
        fmt_value = wb.add_format({
            "font_name": FONT, "font_size": 10,
            "font_color": PRIMARY, "align": "left", "valign": "vcenter"
        })
        fmt_section = wb.add_format({
            "font_name": FONT, "font_size": 11, "bold": True,
            "bg_color": SECONDARY, "font_color": PRIMARY,
            "align": "center", "valign": "vcenter", "border": 1
        })
        fmt_th = wb.add_format({
            "font_name": FONT, "font_size": 10, "bold": True,
            "bg_color": SECONDARY, "font_color": PRIMARY,
            "align": "center", "valign": "vcenter", "border": 1, "text_wrap": True
        })
        fmt_td = wb.add_format({
            "font_name": FONT, "font_size": 10,
            "align": "left", "valign": "vcenter", "border": 1
        })
        fmt_td_num = wb.add_format({
            "font_name": FONT, "font_size": 10,
            "align": "right", "valign": "vcenter", "border": 1,
            "num_format": "#,##0"
        })
        fmt_td_dec = wb.add_format({
            "font_name": FONT, "font_size": 10,
            "align": "right", "valign": "vcenter", "border": 1,
            "num_format": "0.00"
        })
        fmt_td_money = wb.add_format({
            "font_name": FONT, "font_size": 10,
            "align": "right", "valign": "vcenter", "border": 1,
            "num_format": "$#,##0.00"
        })
        fmt_terms_hdr = wb.add_format({
            "font_name": FONT, "font_size": 11, "bold": True,
            "font_color": PRIMARY, "align": "left", "valign": "vcenter"
        })
        fmt_terms_lbl = wb.add_format({
            "font_name": FONT, "font_size": 9, "bold": True,
            "font_color": PRIMARY, "align": "left", "valign": "top"
        })
        fmt_terms_val = wb.add_format({
            "font_name": FONT, "font_size": 9,
            "font_color": PRIMARY, "align": "left", "valign": "top", "text_wrap": True
        })
        fmt_box = wb.add_format({"border": 1})
        fmt_money_big = wb.add_format({
            "font_name": FONT, "font_size": 11, "bold": True,
            "align": "right", "valign": "vcenter", "border": 1,
            "num_format": "$#,##0.00"
        })
        fmt_label_right = wb.add_format({
            "font_name": FONT, "font_size": 10, "bold": True,
            "align": "right", "valign": "vcenter"
        })
        fmt_value_right = wb.add_format({
            "font_name": FONT, "font_size": 10,
            "align": "right", "valign": "vcenter"
        })

        # ===== Layout settings =====
        ws.set_zoom(100)
        ws.set_default_row(15)

        # Column widths (A..N aprox)
        ws.set_column("A:A", 18)
        ws.set_column("B:B", 34)
        ws.set_column("C:C", 18)
        ws.set_column("D:D", 18)
        ws.set_column("E:E", 16)
        ws.set_column("F:F", 16)
        ws.set_column("G:N", 16)

        # ===== 1) Logo pequeño a la izquierda =====
        ws.set_row(0, 30)
        try:
            ws.insert_image("A1", logo_path, {"x_scale": 0.12, "y_scale": 0.12, "x_offset": 2, "y_offset": 2})
        except Exception:
            pass

        # ===== Title =====
        ws.write("D1", "PACKING LIST", fmt_title)

        # ===== 2) Dirección debajo del logo =====
        ws.write("A5", "7226 NW 56 STREET", fmt_addr)
        ws.write("A6", "MIAMI, FLORIDA 33166", fmt_addr)

        # ===== 3) Client info (col A) + 4) Doc info (col D) =====
        # Row base for info blocks
        r0 = 6  # excel row index (0-based) -> row 7 in UI

        ws.write(r0, 0, "QUOTATION FOR", fmt_label)

        # Left block labels (A)
        left_fields = [
            ("Name:", client_info.get("name", "")),
            ("TIN:", client_info.get("tin", "")),
            ("Phone/Cellphone:", client_info.get("phone", "")),
            ("Address:", client_info.get("address", "")),
            ("Email:", client_info.get("email", "")),
            ("Country:", client_info.get("country", result.get("country", ""))),
        ]

        rr = r0 + 1
        for k, v in left_fields:
            ws.write(rr, 0, k, fmt_label)
            ws.write(rr, 1, v, fmt_value)
            rr += 1

        # Right block (D)
        right_fields = [
            ("Date:", result.get("date", "")),
            ("Quotation#:", result.get("quotation_number", "")),
            ("Client ID:", client_id),
            ("Warehouse Origin:", result.get("warehouse_origin", "")),
            ("Incoterm:", result.get("incoterm", "")),
        ]

        rr2 = r0 + 1
        for k, v in right_fields:
            ws.write(rr2, 3, k, fmt_label)
            ws.write(rr2, 4, v, fmt_value)
            rr2 += 1

        # ===== Determine where table starts =====
        table_start = max(rr, rr2) + 2  # blank line

        # ===== 5) Section header: DIMENSIONS PER UNIT BOX =====
        # We place the section title above the dimension columns:
        # "unidades_dentro_caja" .. "volumen_m3" (if they exist)
        col_index = {c: i for i, c in enumerate(df.columns)}
        dim_cols = ["unidades_dentro_caja", "alto_cm", "ancho_cm", "largo_cm", "peso_neto_kg", "peso_bruto_kg", "volumen_m3"]
        present_dim_cols = [c for c in dim_cols if c in col_index]

        if present_dim_cols:
            c1 = col_index[present_dim_cols[0]]
            c2 = col_index[present_dim_cols[-1]]
            ws.merge_range(table_start, c1, table_start, c2, "DIMENSIONS PER UNIT BOX", fmt_section)

        # Table header row comes right after section row
        th_row = table_start + 1

        # Write table headers
        for c, col in enumerate(df.columns):
            ws.write(th_row, c, col.upper(), fmt_th)

        # Write rows
        for i, row in enumerate(df.itertuples(index=False), start=1):
            r = th_row + i
            for c, col in enumerate(df.columns):
                val = getattr(row, col)
                if col in ("cantidad", "unidades_dentro_caja", "cajas_master_bultos"):
                    ws.write(r, c, val, fmt_td_num)
                elif col in ("precio_unit", "total_usd"):
                    ws.write(r, c, val, fmt_td_money)
                elif col in ("peso_neto_kg", "peso_bruto_kg", "peso_bruto_total_kg", "volumen_m3", "volumen_total_m3"):
                    ws.write(r, c, val, fmt_td_dec)
                else:
                    ws.write(r, c, val, fmt_td)

        # Freeze panes below header
        ws.freeze_panes(th_row + 1, 0)

        # ===== 6) Subtotal / Tax / Total block =====
        data_last_row = th_row + len(df)  # last data row index
        summary_start = data_last_row + 2

        # We place summary on right side (like your example)
        # Use columns E/F if exist, else last two columns
        col_money = col_index.get("total_usd", max(1, len(df.columns) - 1))
        col_label = max(0, col_money - 1)

        ws.write(summary_start,     col_label, "SUBTOTAL", fmt_label_right)
        ws.write_number(summary_start, col_money, subtotal, fmt_money_big)

        ws.write(summary_start + 1, col_label, "TAX RATE", fmt_label_right)
        ws.write(summary_start + 1, col_money, f"{int(tax_rate*100)}%", fmt_value_right)

        ws.write(summary_start + 2, col_label, "SALES TAX", fmt_label_right)
        ws.write_number(summary_start + 2, col_money, sales_tax, fmt_money_big)

        ws.write(summary_start + 3, col_label, "TOTAL", fmt_label_right)
        ws.write_number(summary_start + 3, col_money, grand_total, fmt_money_big)

        # ===== 7) Commercial terms =====
        terms_start = summary_start + 6
        ws.write(terms_start, 0, "COMMERCIAL TERMS", fmt_terms_hdr)

        terms = [
            ("PAYMENT METHOD:", "Payment should be deposited to the Bank of America account # 2290 5782 0399. Routing # 063100277 / SWIFT # BOFAUS3M. SAT AMERICA INC or Credit Card"),
            ("DELIVERY TIME:", "For cash payments the delivery time is 3 days after the payment confirmation. For companies with credit the delivery time is 3 days after the purchase"),
            ("PLACE OF DELIVERY:", "The place of delivery will be in the warehouse of SAT AMERICA located in 7226 NW 56 STREET, MIAMI FLORIDA 33166; in case the products need to be sent to a different location the client will inform and coordinate the means of transport and authorize the person or company in charge of the transportation of the products."),
            ("DELIVERY SCHEDULE", "The delivery schedule will be on weekdays (Monday to Friday) from 9am to 1pm and from 2pm to 5pm."),
            ("WARRANTY", "For a warranty claim customer will need to ask for the RMA format and keep the invoice."),
            ("RELATED TAX:", 'If "Related Tax" does not match your CUSTOMS information please let us know'),
        ]

        rr = terms_start + 1
        for k, v in terms:
            ws.write(rr, 0, k, fmt_terms_lbl)
            ws.merge_range(rr, 1, rr, 10, v, fmt_terms_val)
            rr += 1

        # ===== 8) Salesperson last line =====
        rr += 1
        ws.write(rr, 0, "SALESPERSON:", fmt_label)
        ws.write(rr, 1, salesperson, fmt_value)

        # ===== Totales sheet (keep, uniform font not critical but ok) =====
        totals_df = pd.DataFrame([result.get("totals", {})])
        totals_df.to_excel(writer, index=False, sheet_name="Totales")
        ws2 = writer.sheets["Totales"]
        ws2.set_column(0, 30, 24)



def require_token(x_token: str | None) -> dict:
    if not x_token:
        raise HTTPException(status_code=401, detail="Falta token")

    # ✅ Si Supabase está activo, valida contra tabla users
    if supabase_enabled():
        res = (
            supabase.table("users")
            .select("username, role, countries, clients, is_active")
            .eq("token", x_token)
            .limit(1)
            .execute()
        )
        if not res.data:
            raise HTTPException(status_code=401, detail="Token inválido")
        u = res.data[0]
        if not u.get("is_active", True):
            raise HTTPException(status_code=401, detail="Usuario inactivo")
        return {
            "username": u["username"],
            "role": u.get("role", "user"),
            "countries": u.get("countries", []) or [],
            "clients": u.get("clients", []) or [],
        }

    # ⬇️ Fallback local (si no hay supabase)
    if x_token not in TOKENS:
        raise HTTPException(status_code=401, detail="Token inválido")
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
    client_up = client_id.upper().strip()

    if supabase_enabled():
        res = (
            supabase.table("clients")
            .select("*")
            .eq("client_code", client_up)
            .limit(1)
            .execute()
        )
        if not res.data:
            raise HTTPException(status_code=404, detail=f"Cliente no existe: {client_up}")
        return res.data[0]

    # ⬇️ fallback CSV
    if not CLIENTS_CSV.exists():
        raise HTTPException(status_code=500, detail=f"No existe el archivo: {CLIENTS_CSV}")
    df = pd.read_csv(CLIENTS_CSV).fillna("")
    if "cliente" not in df.columns:
        raise HTTPException(status_code=500, detail="clients.csv debe tener columna 'cliente'")
    df["cliente"] = df["cliente"].astype(str).str.upper().str.strip()
    row = df[df["cliente"] == client_up]
    if row.empty:
        raise HTTPException(status_code=404, detail=f"Cliente no existe en clients.csv: {client_up}")
    return row.iloc[0].to_dict()

def get_current_user(x_token: str = Header(None, alias="X-Token")):
    if not x_token:
        raise HTTPException(status_code=401, detail="Missing X-Token")

    # token = username (según tu tabla public.users)
    res = supabase.table("users").select("*").eq("username", x_token).limit(1).execute()
    if not res.data:
        raise HTTPException(status_code=401, detail="Invalid X-Token")

    return res.data[0]


# --------- Endpoints ----------
@app.get("/")
def home():
    return {"mensaje": "App funcionando 🚀", "next": "Ve a /docs"}

@app.post("/auth/login")
def login(data: LoginIn):
    username = (data.username or "").strip().lower()
    password = (data.password or "").strip()

    if not username or not password:
        raise HTTPException(status_code=400, detail="Faltan credenciales")

    if supabase_enabled():
        res = (
            supabase.table("users")
            .select("token, username, password, role, countries, clients, is_active")
            .eq("username", username)
            .limit(1)
            .execute()
        )
        if not res.data:
            raise HTTPException(status_code=401, detail="Usuario o clave incorrectos")

        u = res.data[0]
        if not u.get("is_active", True):
            raise HTTPException(status_code=401, detail="Usuario inactivo")

        if str(u.get("password", "")) != password:
            raise HTTPException(status_code=401, detail="Usuario o clave incorrectos")

        return {
            "token": u["token"],
            "username": u["username"],
            "role": u.get("role", "user"),
            "countries": u.get("countries", []) or [],
            "clients": u.get("clients", []) or [],
        }

    # ⬇️ Fallback local
    u = USERS.get(username)
    if not u or u.get("password") != password:
        raise HTTPException(status_code=401, detail="Usuario o clave incorrectos")

    token = f"token-{username}"
    TOKENS[token] = username
    return {"token": token, "username": username, **u}



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
        "client_code": client_up,
        "port": (port.strip() if port else None),
        "count": int(len(merged)),
        "priced_count": int(merged["has_price"].sum()),
        "products": merged.to_dict(orient="records"),
    }

@app.post("/packing-list/{country_code}")
def create_packing_list(
    country_code: str,
    client: str,
    port: str,
    payload: PackingListIn,
    x_token: str | None = Header(default=None, alias="X-Token"),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    user = require_token(x_token)

    require_permission(user, "products:read")
    enforce_role_country(user, country_code.upper())

    # 1) Normalizar
    country_up = country_code.upper().strip()
    client_up = client.upper().strip()
    port_up = port.strip()

    # 2) Cliente existe
    client_info = get_client_record(client_up)

    # 3) Permisos cliente
    allowed_clients = [c.upper() for c in user.get("clients", [])]
    if "ALL" not in allowed_clients and client_up not in allowed_clients:
        raise HTTPException(status_code=403, detail="No tienes acceso a este cliente")

    # 🔒 Idempotency (no regenerar)
    if idempotency_key:
        hit = (
            supabase.table("packing_lists")
            .select("pl_id")
            .eq("idempotency_key", str(idempotency_key))
            .limit(1)
            .execute()
        )
        if hit.data:
            return get_packing_list(hit.data[0]["pl_id"], x_token)

    # ===== PRODUCTS (Supabase) =====
    prod_res = supabase.table("products").select("*").execute()
    prod = pd.DataFrame(prod_res.data).fillna("")
    if prod.empty:
        raise HTTPException(status_code=500, detail="Tabla products vacía")
    prod["stone"] = prod["stone"].astype(str).str.strip()

    # ===== PRICES (Supabase) =====
    prices_res = (
        supabase.table("prices")
        .select("*")
        .eq("country", country_up)
        .eq("client_code", client_up)
        .eq("port", port_up)
        .execute()
    )
    pr_f = pd.DataFrame(prices_res.data).fillna("")
    if pr_f.empty:
        raise HTTPException(
            status_code=404,
            detail=f"No hay precios para country={country_up}, client={client_up}, port={port_up}",
        )
    pr_f["sku_stone"] = pr_f["sku_stone"].astype(str).str.strip()
    price_map = {row["sku_stone"]: row for _, row in pr_f.iterrows()}

    rows = []
    total_usd = 0.0
    total_cajas = 0
    total_peso_bruto = 0.0
    total_volumen = 0.0

    # ===== CALCULO =====
    for item in payload.items:
        sku = str(item.sku_stone).strip()
        qty = int(item.quantity)

        p_row = prod[prod["stone"] == sku]
        if p_row.empty:
            raise HTTPException(status_code=404, detail=f"SKU no existe: {sku}")
        p = p_row.iloc[0].to_dict()

        if sku not in price_map:
            raise HTTPException(status_code=404, detail=f"No hay precio para SKU {sku}")
        prc = price_map[sku]

        unidades_caja = int(p.get("u_por_caja", 0) or 0)
        if unidades_caja <= 0:
            raise HTTPException(status_code=422, detail=f"u_por_caja inválido para {sku}")

        cajas = ceil(qty / unidades_caja)

        peso_bruto_caja = float(p.get("peso_bruto_kg", 0) or 0)
        cbm_caja = float(p.get("cbm", 0) or 0)

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
            "puerto": port_up,
        })

    # ===== GUARDADO =====
    pl_id = next_pl_id()
    quotation_number = next_quotation_number()
    today_str = datetime.now().strftime("%Y-%m-%d")

    excel_file = OUTPUT_DIR / f"{pl_id}_{country_up}_{client_up}_{port_up}.xlsx"

    items_count = len(payload.items)
    total_units = sum(int(i.quantity) for i in payload.items)

    result_out = {
        "pl_id": pl_id,
        "date": today_str,
        "country": country_up,
        "client_code": client_up,
        "port": port_up,
        "created_by": user["username"],
        "quotation_number": quotation_number,
        "warehouse_origin": payload.warehouse_origin or "",
        "incoterm": payload.incoterm or "",
        "client_info": client_info,
        "items_count": items_count,
        "total_units": total_units,
        "excel_file": str(excel_file),
        "totals": {
            "total_usd": total_usd,
            "total_cajas_master": total_cajas,
            "total_peso_bruto_kg": total_peso_bruto,
            "total_volumen_m3": total_volumen,
        },
        "rows": rows,
    }

    build_packing_list_excel(result_out, excel_file)

    # Guardar PL en Supabase
    supabase.table("packing_lists").insert({
        "pl_id": pl_id,
        "date": today_str,
        "country": country_up,
        "client_code": client_up,
        "port": port_up,
        "created_by": user["username"],
        "quotation_number": quotation_number,
        "warehouse_origin": payload.warehouse_origin or "",
        "incoterm": payload.incoterm or "",
        "items_count": items_count,
        "total_units": total_units,
        "excel_file": str(excel_file),
        "idempotency_key": idempotency_key or "",
    }).execute()

        # Guardar items del Packing List
    def pick(r, *keys, default=None):
        for k in keys:
            if k in r and r[k] not in (None, "", "EMPTY"):
                return r[k]
        return default

    def to_int(v, default=0):
        try:
            if v in (None, "", "EMPTY"):
                return default
            return int(float(v))
        except:
            return default

    def to_float(v, default=0.0):
        try:
            if v in (None, "", "EMPTY"):
                return default
            return float(v)
        except:
            return default


    items_to_insert = []

    for r in rows:
        items_to_insert.append({
            "pl_id": pl_id,
            "sku_stone": str(pick(r, "sku_stone", "stone", "sku", "sku_datatech", default="")).strip(),
            "quantity": to_int(pick(r, "quantity", "qty", "cantidad")),
            "price_unit": to_float(pick(r, "price_unit", "precio_unit", "unit_price")),
            "total_line": to_float(pick(r, "total_line", "total_usd", "line_total")),
            "u_por_caja": to_int(pick(r, "u_por_caja", "unidades_dentro_caja")),
            "cajas_master_bultos": to_int(pick(r, "cajas_master_bultos")),
            "peso_bruto_total_kg": to_float(pick(r, "peso_bruto_total_kg")),
            "volumen_total_m3": to_float(pick(r, "volumen_total_m3")),
        })

    if items_to_insert:
        supabase.table("packing_list_items").insert(items_to_insert).execute()



    return result_out





@app.get("/packing-lists")
def list_packing_lists(x_token: str | None = Header(default=None, alias="X-Token")):
    user = require_token(x_token)

    # Supabase
    if supabase_enabled():
        res = supabase.table("packing_lists").select("*").execute()
        rows = res.data or []

        # Permisos
        if user.get("role") != "admin":
            allowed_clients = [c.upper() for c in user.get("clients", [])]
            if "ALL" not in allowed_clients:
                rows = [r for r in rows if str(r.get("client", "")).upper() in allowed_clients]

            allowed_countries = [c.upper() for c in user.get("countries", [])]
            if "ALL" not in allowed_countries:
                rows = [r for r in rows if str(r.get("country", "")).upper() in allowed_countries]

        return {"count": int(len(rows)), "packing_lists": rows}

    # Fallback CSV
    ensure_packing_lists_csv()
    df = pd.read_csv(PACKING_LISTS_CSV).fillna("")

    if user.get("role") != "admin":
        allowed_clients = [c.upper() for c in user.get("clients", [])]
        if "ALL" not in allowed_clients:
            df["client"] = df["client"].astype(str).str.upper()
            df = df[df["client"].isin(allowed_clients)]

        allowed_countries = [c.upper() for c in user.get("countries", [])]
        if "ALL" not in allowed_countries:
            df["country"] = df["country"].astype(str).str.upper()
            df = df[df["country"].isin(allowed_countries)]

    return {"count": int(len(df)), "packing_lists": df.to_dict(orient="records")}

@app.get("/packing-lists/{pl_id}")
def get_packing_list(pl_id: str, x_token: str | None = Header(default=None, alias="X-Token")):
    user = require_token(x_token)

    # 1) Header
    header_res = (
        supabase.table("packing_lists")
        .select("*")
        .eq("pl_id", pl_id)
        .limit(1)
        .execute()
    )

    if not header_res.data:
        raise HTTPException(status_code=404, detail="Packing list not found")

    header = header_res.data[0]

    # 2) Seguridad (misma lógica que ya usas)
    enforce_role_country(user, header.get("country"))

    if user.get("role") != "admin":
        allowed_clients = user.get("clients") or []
        if allowed_clients != ["ALL"] and header.get("client_code") not in allowed_clients:
            raise HTTPException(status_code=403, detail="Not allowed for this client")

    # 3) Items
    items_res = (
        supabase.table("packing_list_items")
        .select("*")
        .eq("pl_id", pl_id)
        .order("created_at")
        .execute()
    )

    return {
        "header": header,
        "items": items_res.data or []
    }


from fastapi.responses import FileResponse
from pathlib import Path

@app.get("/packing-lists/{pl_id}/excel")
def download_packing_list_excel(
    pl_id: str,
    x_token: str | None = Header(default=None, alias="X-Token")
):
    user = require_token(x_token)

    # 1) Validar que exista el PL
    header_res = (
        supabase.table("packing_lists")
        .select("*")
        .eq("pl_id", pl_id)
        .limit(1)
        .execute()
    )

    if not header_res.data:
        raise HTTPException(status_code=404, detail="Packing list not found")

    header = header_res.data[0]

    # 2) Seguridad
    enforce_role_country(user, header.get("country"))

    # 3) Ruta del archivo (ya guardada al crear el PL)
    excel_path = Path(header["excel_file"])

    if not excel_path.exists():
        raise HTTPException(status_code=404, detail="Excel file not found on server")

    # 4) Descargar
    return FileResponse(
        path=excel_path,
        filename=excel_path.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


@app.get("/packing-lists-ui")
def list_packing_lists_ui(x_token: str | None = Header(default=None, alias="X-Token")):
    user = require_token(x_token)
    out = list_packing_lists(x_token=x_token)

    rows = out.get("packing_lists", out)  # por si list_packing_lists ya devuelve lista
    return {"count": len(rows), "data": rows}


@app.post("/packing-list/{country_code}/excel")
def packing_list_excel(
    country_code: str,
    data: PackingListIn,
    client: str,
    port: str,
    x_token: str | None = Header(default=None, alias="X-Token"),
):
    result = create_packing_list(
        country_code=country_code,
        data=data,
        client=client,
        port=port,
        x_token=x_token,
    )

    excel_path = Path(result["excel_file"])
    if not excel_path.exists():
        raise HTTPException(status_code=500, detail="No se encontró el Excel generado")

    f = open(excel_path, "rb")
    filename = excel_path.name

    return StreamingResponse(
        f,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


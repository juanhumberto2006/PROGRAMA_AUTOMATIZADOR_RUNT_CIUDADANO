# -*- coding: utf-8 -*-
"""
Módulo de Seguridad, Autenticación y Gestión de Tokens en SQLite (Expiración estricta de 60 minutos).
- Almacena usuarios con contraseñas cifradas (PBKDF2-HMAC-SHA256 + salt aleatorio).
- Genera tokens criptográficos de sesión con vigencia exacta de 60 minutos (3600 segundos).
- Al abrir o cerrar el programa, invalida cualquier sesión previa para exigir siempre nuevo inicio de sesión.
- Valida el token antes de cada consulta o acción y permite cerrar sesión manualmente en cualquier momento.
"""

import os
import hmac
import secrets
import hashlib
import sqlite3
import datetime

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(CURRENT_DIR, "progreso_consultas.db")

SESSION_DURATION_MINUTES = 60


def _get_conn():
    return sqlite3.connect(DB_PATH)


def hash_password(password: str, salt: str = None):
    if not salt:
        salt = secrets.token_hex(16)
    pwd_hash = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        120000
    ).hex()
    return pwd_hash, salt


def verify_password(password: str, stored_hash: str, salt: str) -> bool:
    calc_hash, _ = hash_password(password, salt)
    return hmac.compare_digest(calc_hash, stored_hash)


def init_auth_db():
    conn = _get_conn()
    cur = conn.cursor()

    # Tabla de Usuarios
    cur.execute("""
        CREATE TABLE IF NOT EXISTS usuarios_sistema (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            nombre_completo TEXT NOT NULL,
            rol TEXT NOT NULL DEFAULT 'ADMIN',
            password_hash TEXT NOT NULL,
            password_salt TEXT NOT NULL,
            activo INTEGER NOT NULL DEFAULT 1,
            intentos_fallidos INTEGER NOT NULL DEFAULT 0,
            bloqueado_hasta TEXT,
            fecha_creacion TEXT NOT NULL
        )
    """)

    # Tabla de Sesiones y Tokens (vigencia de 60 minutos)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sesiones_token (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT UNIQUE NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            fecha_inicio TEXT NOT NULL,
            fecha_expiracion TEXT NOT NULL,
            activa INTEGER NOT NULL DEFAULT 1,
            motivo_cierre TEXT,
            FOREIGN KEY(user_id) REFERENCES usuarios_sistema(id)
        )
    """)

    # Tabla de Auditoría de Accesos
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bitacora_accesos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT,
            evento TEXT,
            detalle TEXT,
            fecha_evento TEXT
        )
    """)

    # Crear usuarios iniciales por defecto si no existen
    cur.execute("SELECT COUNT(*) FROM usuarios_sistema")
    if cur.fetchone()[0] == 0:
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        default_users = [
            ("juan", "Juan - Validación RUNT", "ADMIN", "Juan2026*"),
            ("admin", "Administrador General RUNT", "ADMIN", "AdminRunt2026*")
        ]
        for uname, full_name, role, raw_pwd in default_users:
            p_hash, p_salt = hash_password(raw_pwd)
            cur.execute("""
                INSERT INTO usuarios_sistema (
                    username, nombre_completo, rol, password_hash, password_salt, activo, fecha_creacion
                ) VALUES (?, ?, ?, ?, ?, 1, ?)
            """, (uname, full_name, role, p_hash, p_salt, now_str))

    conn.commit()
    conn.close()


def registrar_evento(username: str, evento: str, detalle: str):
    try:
        conn = _get_conn()
        cur = conn.cursor()
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur.execute("""
            INSERT INTO bitacora_accesos (username, evento, detalle, fecha_evento)
            VALUES (?, ?, ?, ?)
        """, (username, evento, detalle, now_str))
        conn.commit()
        conn.close()
    except Exception:
        pass


def invalidate_all_active_sessions(motivo="CIERRE_O_REINICIO_APLICACION"):
    """
    Se ejecuta al iniciar y al cerrar el programa para garantizar que ninguna sesión
    anterior quede abierta y siempre se deba iniciar sesión de nuevo.
    """
    init_auth_db()
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("""
        UPDATE sesiones_token
        SET activa = 0, motivo_cierre = ?
        WHERE activa = 1
    """, (motivo,))
    conn.commit()
    conn.close()


def authenticate_user(username: str, password: str):
    """
    Valida credenciales, controla intentos fallidos y emite un token válido por 60 minutos exactos.
    Retorna: (exito: bool, mensaje: str, session_data: dict | None)
    """
    init_auth_db()
    uname = (username or "").strip().lower()
    pwd = password or ""

    if not uname or not pwd:
        return False, "Debes ingresar tanto el usuario como la contraseña.", None

    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, username, nombre_completo, rol, password_hash, password_salt,
               activo, intentos_fallidos, bloqueado_hasta
        FROM usuarios_sistema
        WHERE LOWER(username) = ?
    """, (uname,))
    row = cur.fetchone()

    now = datetime.datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    if not row:
        conn.close()
        registrar_evento(uname, "LOGIN_FALLIDO", "Usuario no existe")
        return False, "Usuario o contraseña incorrectos.", None

    uid, db_user, full_name, rol, p_hash, p_salt, activo, intentos, bloqueado_hasta = row

    if not activo:
        conn.close()
        registrar_evento(db_user, "LOGIN_RECHAZADO", "Cuenta inactiva")
        return False, "Esta cuenta de usuario se encuentra deshabilitada.", None

    # Verificar bloqueo temporal por intentos fallidos
    if bloqueado_hasta:
        try:
            dt_bloq = datetime.datetime.strptime(bloqueado_hasta, "%Y-%m-%d %H:%M:%S")
            if now < dt_bloq:
                restante = int((dt_bloq - now).total_seconds())
                conn.close()
                return False, f"Cuenta bloqueada temporalmente por seguridad. Intenta en {restante} segundos.", None
        except Exception:
            pass

    if not verify_password(pwd, p_hash, p_salt):
        nuevos_intentos = (intentos or 0) + 1
        bloq_str = None
        msg = f"Contraseña incorrecta (Intento {nuevos_intentos} de 5)."
        if nuevos_intentos >= 5:
            dt_lock = now + datetime.timedelta(minutes=2)
            bloq_str = dt_lock.strftime("%Y-%m-%d %H:%M:%S")
            nuevos_intentos = 0
            msg = "Demasiados intentos fallidos. Cuenta bloqueada por 2 minutos."

        cur.execute("""
            UPDATE usuarios_sistema
            SET intentos_fallidos = ?, bloqueado_hasta = ?
            WHERE id = ?
        """, (nuevos_intentos, bloq_str, uid))
        conn.commit()
        conn.close()
        registrar_evento(db_user, "LOGIN_FALLIDO", msg)
        return False, msg, None

    # Credenciales válidas: limpiar intentos e invalidar sesiones previas del usuario
    cur.execute("""
        UPDATE usuarios_sistema
        SET intentos_fallidos = 0, bloqueado_hasta = NULL
        WHERE id = ?
    """, (uid,))

    cur.execute("""
        UPDATE sesiones_token
        SET activa = 0, motivo_cierre = 'NUEVO_INICIO_SESION'
        WHERE user_id = ? AND activa = 1
    """, (uid,))

    # Emitir nuevo token con vencimiento exacto en 60 minutos
    token = secrets.token_urlsafe(32)
    exp_dt = now + datetime.timedelta(minutes=SESSION_DURATION_MINUTES)
    exp_str = exp_dt.strftime("%Y-%m-%d %H:%M:%S")

    cur.execute("""
        INSERT INTO sesiones_token (
            token, user_id, username, fecha_inicio, fecha_expiracion, activa, motivo_cierre
        ) VALUES (?, ?, ?, ?, ?, 1, NULL)
    """, (token, uid, db_user, now_str, exp_str))

    conn.commit()
    conn.close()

    registrar_evento(db_user, "LOGIN_EXITOSO", f"Token emitido. Expira: {exp_str}")

    return True, "Autenticación exitosa.", {
        "token": token,
        "user_id": uid,
        "username": db_user,
        "nombre_completo": full_name,
        "rol": rol,
        "fecha_inicio": now_str,
        "fecha_expiracion": exp_str,
        "exp_timestamp": exp_dt.timestamp()
    }


def validate_session_token(token: str):
    """
    Verifica en SQLite si el token existe, sigue activo y no ha superado los 60 minutos.
    Retorna: (valido: bool, segundos_restantes: int, mensaje: str)
    """
    if not token:
        return False, 0, "No hay sesión activa."

    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT username, fecha_expiracion, activa
        FROM sesiones_token
        WHERE token = ?
    """, (token,))
    row = cur.fetchone()

    if not row:
        conn.close()
        return False, 0, "Token de sesión inválido o inexistente."

    uname, exp_str, activa = row
    if not activa:
        conn.close()
        return False, 0, "La sesión fue cerrada."

    now = datetime.datetime.now()
    exp_dt = datetime.datetime.strptime(exp_str, "%Y-%m-%d %H:%M:%S")
    remaining = int((exp_dt - now).total_seconds())

    if remaining <= 0:
        cur.execute("""
            UPDATE sesiones_token
            SET activa = 0, motivo_cierre = 'EXPIRACION_60_MINUTOS'
            WHERE token = ?
        """, (token,))
        conn.commit()
        conn.close()
        registrar_evento(uname, "SESION_EXPIRADA", "El token cumplió 60 minutos de vigencia.")
        return False, 0, "Tu sesión de 60 minutos ha expirado por seguridad. Inicia sesión nuevamente."

    conn.close()
    return True, remaining, "Sesión activa"


def logout_session(token: str, motivo="LOGOUT_MANUAL_USUARIO"):
    if not token:
        return
    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("SELECT username FROM sesiones_token WHERE token = ?", (token,))
    row = cur.fetchone()
    uname = row[0] if row else "desconocido"

    cur.execute("""
        UPDATE sesiones_token
        SET activa = 0, motivo_cierre = ?
        WHERE token = ?
    """, (motivo, token))
    conn.commit()
    conn.close()
    registrar_evento(uname, "LOGOUT", motivo)


def create_or_update_user(username: str, full_name: str, password: str, rol: str = "OPERADOR"):
    init_auth_db()
    uname = (username or "").strip().lower()
    if len(uname) < 3:
        return False, "El nombre de usuario debe tener al menos 3 caracteres."
    if len(password or "") < 6:
        return False, "La contraseña debe tener mínimo 6 caracteres."

    p_hash, p_salt = hash_password(password)
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = _get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO usuarios_sistema (
            username, nombre_completo, rol, password_hash, password_salt, activo, fecha_creacion
        ) VALUES (?, ?, ?, ?, ?, 1, ?)
        ON CONFLICT(username) DO UPDATE SET
            nombre_completo = excluded.nombre_completo,
            rol = excluded.rol,
            password_hash = excluded.password_hash,
            password_salt = excluded.password_salt,
            activo = 1,
            intentos_fallidos = 0,
            bloqueado_hasta = NULL
    """, (uname, full_name.strip() or uname, rol, p_hash, p_salt, now_str))
    conn.commit()
    conn.close()
    registrar_evento(uname, "USUARIO_ACTUALIZADO", f"Usuario creado/actualizado ({rol})")
    return True, f"Usuario '{uname}' guardado exitosamente."

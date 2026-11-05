# -*- coding: utf-8 -*-
"""
Escáner de Patios y Gestión de Estado (SQLite) para el Automatizador RUNT Ciudadano.
Lee directamente la estructura de carpetas:
G:\\martha garcia\\VEHICULOS RETENIDOS VALIDAR RUNT____\\[PATIO]\\AÑO YYYY\\[PLACA] - [PROPIETARIO]
Sin alterar los archivos Excel originales ni las capturas raíz.
"""

import os
import json
import sqlite3
import datetime
import openpyxl

BASE_ROOT = r"G:\martha garcia\VEHICULOS RETENIDOS VALIDAR RUNT____"
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(CURRENT_DIR, "progreso_consultas.db")

EXCLUDED_FOLDERS = {
    "PROGRAMA_GENERADOR_RUNT",
    "PROGRAMA_AUTOMATIZADOR_RUNT_CIUDADANO",
    "RESPALDO",
    "RUNT",
    "__pycache__"
}

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS historial_consultas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patio TEXT,
            anio TEXT,
            placa TEXT,
            propietario TEXT,
            tipo_doc TEXT,
            nro_doc TEXT,
            estado_consulta TEXT,
            tiene_embargo_limitacion TEXT,
            detalle_limitacion TEXT,
            soat_estado TEXT,
            rtm_estado TEXT,
            capturas_guardadas INTEGER,
            ruta_expediente TEXT,
            fecha_actualizacion TEXT,
            UNIQUE(patio, anio, placa)
        )
    """)
    conn.commit()
    conn.close()

def list_patios():
    if not os.path.exists(BASE_ROOT):
        return []
    patios = []
    for d in sorted(os.listdir(BASE_ROOT)):
        full = os.path.join(BASE_ROOT, d)
        if os.path.isdir(full) and d not in EXCLUDED_FOLDERS and not d.startswith("."):
            patios.append(d)
    return patios

def list_anios(patio):
    patio_dir = os.path.join(BASE_ROOT, patio)
    if not os.path.exists(patio_dir):
        return []
    anios = []
    for d in sorted(os.listdir(patio_dir)):
        full = os.path.join(patio_dir, d)
        if os.path.isdir(full) and d.upper().startswith("AÑO"):
            anios.append(d)
    return anios

def inspect_expediente_folder(exp_dir, placa):
    """
    Revisa los archivos existentes dentro de EXPEDIENTE VEHICULAR para saber si:
    - Ya tiene Constancia Sin Propietario
    - Ya tiene Inconsistencia de Propietario
    - Ya tiene las capturas del RUNT Ciudadano
    - Está pendiente (vacía)
    """
    if not os.path.exists(exp_dir):
        return "PENDIENTE", 0, ""
    files = [f for f in os.listdir(exp_dir) if f.lower().endswith(".png")]
    if not files:
        return "PENDIENTE", 0, ""

    upper_files = " ".join(files).upper()
    if "CONSTANCIA_RUNT_SIN_PROPIETARIO" in upper_files and "RUNT_CIUDADANO" not in upper_files:
        return "SIN_PROPIETARIO_CONSTANCIA", len(files), "Constancia Ley 1730 generada"
    if "INCONSISTENCIA_PROPIETARIO" in upper_files:
        return "INCONSISTENCIA_PROPIETARIO", len(files), "No corresponde con propietarios activos"
    if "RUNT_CIUDADANO_INFO_GENERAL" in upper_files or len(files) >= 3:
        alerta = "NO"
        if "EMBARGO" in upper_files or "HURTO" in upper_files or "FISCALIA" in upper_files:
            alerta = "SI (Ver captura Limitaciones)"
        elif "GARANTIAS" in upper_files:
            alerta = "GARANTIA / PRENDA"
        return "COMPLETADO_CIUDADANO", len(files), alerta

    return "PENDIENTE", len(files), ""

def normalize_doc_type_for_runt(raw_tipo, nro_doc, propietario):
    """
    Convierte el tipo de documento del Excel ('C.C.', 'NIT', 'C.E.', etc.)
    a la opción exacta del desplegable del portal RUNT Ciudadano:
    - 'Cédula Ciudadanía'
    - 'NIT'
    - 'Cédula de Extranjería'
    - 'Pasaporte'
    - 'Tarjeta de Identidad'
    - 'Permiso por Protección Temporal'
    """
    t = str(raw_tipo or "").strip().upper()
    p = str(propietario or "").strip().upper()
    if "NIT" in t or any(k in p for k in ["S.A.S", "S.A.", "LTDA", "LIMITADA", "EMPRESA", "BANCO", "FINANCIERA", "COOPERATIVA"]):
        return "NIT"
    if "C.E" in t or "EXTRANJER" in t:
        return "Cédula de Extranjería"
    if "T.I" in t or "TARJETA" in t:
        return "Tarjeta de Identidad"
    if "PASAPORTE" in t or "PA" == t:
        return "Pasaporte"
    if "PPT" in t or "PERMISO" in t:
        return "Permiso por Protección Temporal"
    return "Cédula Ciudadanía"

def scan_patio_anio(patio, anio):
    """
    Escanea todas las carpetas de vehículos dentro de un Patio y Año.
    Retorna un diccionario con estadísticas y la lista completa de vehículos.
    """
    init_db()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT placa, estado_consulta, tiene_embargo_limitacion, detalle_limitacion, soat_estado, rtm_estado
        FROM historial_consultas
        WHERE patio = ? AND anio = ?
    """, (patio, anio))
    db_map = {row[0]: row[1:] for row in cur.fetchall()}
    conn.close()

    anio_dir = os.path.join(BASE_ROOT, patio, anio)
    if not os.path.exists(anio_dir):
        return {"vehicles": [], "stats": {}}

    # Cargar índice rápido desde RUNT/vehiculos_con_propietario.json si existe
    json_map = {}
    json_path = os.path.join(BASE_ROOT, patio, "RUNT", "vehiculos_con_propietario.json")
    if os.path.exists(json_path):
        try:
            with open(json_path, "r", encoding="utf-8") as jf:
                for item in json.load(jf):
                    p_key = str(item.get("placa") or "").strip().upper()
                    if p_key:
                        json_map[p_key] = item
        except Exception:
            pass

    folders = sorted([f for f in os.listdir(anio_dir) if os.path.isdir(os.path.join(anio_dir, f))])
    vehicles = []
    stats = {
        "total": len(folders),
        "pendientes_con_propietario": 0,
        "completados_ciudadano": 0,
        "inconsistencias": 0,
        "sin_propietario_constancia": 0,
        "con_embargo_alerta": 0
    }

    for folder_name in folders:
        folder_path = os.path.join(anio_dir, folder_name)
        exp_dir = os.path.join(folder_path, "EXPEDIENTE VEHICULAR")
        os.makedirs(exp_dir, exist_ok=True)

        parts = folder_name.split(" - ", 1)
        placa = parts[0].strip().upper()
        prop_folder = parts[1].strip() if len(parts) > 1 else ""

        clase = ""
        marca = ""
        linea = ""
        modelo = ""
        prop_excel = prop_folder
        tipo_doc_raw = "C.C."
        nro_doc = ""

        # 1. Intentar desde el JSON en memoria (instantáneo, <0.05s para 1,700 carpetas)
        if placa in json_map:
            j_info = json_map[placa]
            clase = str(j_info.get("clase") or "").strip()
            marca = str(j_info.get("marca") or "").strip()
            linea = str(j_info.get("linea") or "").strip()
            modelo = str(j_info.get("modelo") or "").strip()
            prop_excel = str(j_info.get("nombres_apellidos") or prop_folder).strip()
            tipo_doc_raw = str(j_info.get("tipo_documento") or "Cédula Ciudadanía").strip()
            nro_doc = str(j_info.get("numero_documento") or "").strip()
        else:
            # 2. Si no está en el JSON y tampoco es un SIN PROPIETARIO obvio, leer Excel individual
            upper_f = prop_folder.upper()
            if not any(k in upper_f for k in ["SIN PROPIETARIO", "SIN INFORMACION"]):
                xlsx_files = [x for x in os.listdir(folder_path) if x.lower().endswith(".xlsx") and not x.startswith("~$")]
                if xlsx_files:
                    try:
                        wb = openpyxl.load_workbook(os.path.join(folder_path, xlsx_files[0]), read_only=True, data_only=True)
                        ws = wb.active
                        row_vals = next(ws.iter_rows(min_row=2, max_row=2, values_only=True), None)
                        if row_vals:
                            clase = str(row_vals[1] or "").strip()
                            marca = str(row_vals[2] or "").strip()
                            linea = str(row_vals[3] or "").strip()
                            modelo = str(row_vals[4] or "").strip()
                            prop_excel = str(row_vals[14] or prop_folder).strip()
                            tipo_doc_raw = str(row_vals[15] or "C.C.").strip()
                            nro_doc = str(row_vals[16] or "").strip()
                        wb.close()
                    except Exception:
                        pass

        # Limpiar decimales tipo '12345.0'
        if nro_doc.endswith(".0"):
            nro_doc = nro_doc[:-2]

        estado_fs, num_imgs, alerta_fs = inspect_expediente_folder(exp_dir, placa)
        db_info = db_map.get(placa)

        tiene_embargo = db_info[1] if db_info else ("SI" if "SI" in alerta_fs else "NO")
        detalle_lim = db_info[2] if db_info else alerta_fs
        soat_est = db_info[3] if db_info else "-"
        rtm_est = db_info[4] if db_info else "-"

        upper_prop = prop_excel.upper()
        is_no_owner = (
            "SIN PROPIETARIO" in upper_prop or
            "SIN INFORMACION" in upper_prop or
            nro_doc in ["", "None", "N/A", "0"]
        )

        if estado_fs == "SIN_PROPIETARIO_CONSTANCIA" or (is_no_owner and estado_fs == "PENDIENTE"):
            estado_final = "SIN_PROPIETARIO_CONSTANCIA"
            stats["sin_propietario_constancia"] += 1
        elif estado_fs == "COMPLETADO_CIUDADANO":
            estado_final = "COMPLETADO_CIUDADANO"
            stats["completados_ciudadano"] += 1
            if "SI" in str(tiene_embargo).upper() or "EMBARGO" in str(detalle_lim).upper() or "HURTO" in str(detalle_lim).upper():
                stats["con_embargo_alerta"] += 1
        elif estado_fs == "INCONSISTENCIA_PROPIETARIO":
            estado_final = "INCONSISTENCIA_PROPIETARIO"
            stats["inconsistencias"] += 1
        else:
            estado_final = "PENDIENTE"
            stats["pendientes_con_propietario"] += 1

        tipo_doc_runt = normalize_doc_type_for_runt(tipo_doc_raw, nro_doc, prop_excel)

        vehicles.append({
            "patio": patio,
            "anio": anio,
            "folder_name": folder_name,
            "folder_path": folder_path,
            "exp_dir": exp_dir,
            "placa": placa,
            "propietario": prop_excel,
            "tipo_doc_raw": tipo_doc_raw,
            "tipo_doc_runt": tipo_doc_runt,
            "nro_doc": nro_doc,
            "clase": clase,
            "marca": marca,
            "linea": linea,
            "modelo": modelo,
            "estado": estado_final,
            "num_imgs": num_imgs,
            "tiene_embargo": tiene_embargo,
            "detalle_limitacion": detalle_lim,
            "soat_estado": soat_est,
            "rtm_estado": rtm_est
        })

    return {"vehicles": vehicles, "stats": stats}

def save_consulta_result(patio, anio, placa, propietario, tipo_doc, nro_doc,
                         estado_consulta, tiene_embargo, detalle_limitacion,
                         soat_estado, rtm_estado, capturas_guardadas, ruta_expediente):
    init_db()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute("""
        INSERT INTO historial_consultas (
            patio, anio, placa, propietario, tipo_doc, nro_doc,
            estado_consulta, tiene_embargo_limitacion, detalle_limitacion,
            soat_estado, rtm_estado, capturas_guardadas, ruta_expediente, fecha_actualizacion
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(patio, anio, placa) DO UPDATE SET
            propietario=excluded.propietario,
            tipo_doc=excluded.tipo_doc,
            nro_doc=excluded.nro_doc,
            estado_consulta=excluded.estado_consulta,
            tiene_embargo_limitacion=excluded.tiene_embargo_limitacion,
            detalle_limitacion=excluded.detalle_limitacion,
            soat_estado=excluded.soat_estado,
            rtm_estado=excluded.rtm_estado,
            capturas_guardadas=excluded.capturas_guardadas,
            ruta_expediente=excluded.ruta_expediente,
            fecha_actualizacion=excluded.fecha_actualizacion
    """, (
        patio, anio, placa, propietario, tipo_doc, nro_doc,
        estado_consulta, tiene_embargo, detalle_limitacion,
        soat_estado, rtm_estado, capturas_guardadas, ruta_expediente, now_str
    ))
    conn.commit()
    conn.close()

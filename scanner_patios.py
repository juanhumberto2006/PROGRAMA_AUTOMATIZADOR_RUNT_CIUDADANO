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
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

BASE_ROOT = r"G:\martha garcia\VEHICULOS RETENIDOS VALIDAR RUNT____"
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(CURRENT_DIR, "progreso_consultas.db")
REPORTS_DIR = os.path.join(CURRENT_DIR, "REPORTES_BD")

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
            clase TEXT DEFAULT '',
            marca TEXT DEFAULT '',
            linea TEXT DEFAULT '',
            modelo TEXT DEFAULT '',
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
    # Migración segura de columnas adicionales si la tabla ya existía antes
    cur.execute("PRAGMA table_info(historial_consultas)")
    existing_cols = {row[1] for row in cur.fetchall()}
    for col_name in ["clase", "marca", "linea", "modelo"]:
        if col_name not in existing_cols:
            try:
                cur.execute(f"ALTER TABLE historial_consultas ADD COLUMN {col_name} TEXT DEFAULT ''")
            except Exception:
                pass
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

    # Sincronizar masivamente en SQLite para que la base de datos tenga siempre todo el inventario al día
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        batch_rows = [
            (
                v["patio"], v["anio"], v["placa"], v["propietario"],
                v["tipo_doc_runt"], v["nro_doc"],
                v["clase"], v["marca"], v["linea"], v["modelo"],
                v["estado"], v["tiene_embargo"], v["detalle_limitacion"],
                v["soat_estado"], v["rtm_estado"], v["num_imgs"], v["exp_dir"], now_str
            )
            for v in vehicles
        ]
        cur.executemany("""
            INSERT INTO historial_consultas (
                patio, anio, placa, propietario, tipo_doc, nro_doc,
                clase, marca, linea, modelo,
                estado_consulta, tiene_embargo_limitacion, detalle_limitacion,
                soat_estado, rtm_estado, capturas_guardadas, ruta_expediente, fecha_actualizacion
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(patio, anio, placa) DO UPDATE SET
                propietario=excluded.propietario,
                tipo_doc=excluded.tipo_doc,
                nro_doc=excluded.nro_doc,
                clase=CASE WHEN excluded.clase != '' THEN excluded.clase ELSE historial_consultas.clase END,
                marca=CASE WHEN excluded.marca != '' THEN excluded.marca ELSE historial_consultas.marca END,
                linea=CASE WHEN excluded.linea != '' THEN excluded.linea ELSE historial_consultas.linea END,
                modelo=CASE WHEN excluded.modelo != '' THEN excluded.modelo ELSE historial_consultas.modelo END,
                estado_consulta=excluded.estado_consulta,
                tiene_embargo_limitacion=CASE
                    WHEN historial_consultas.tiene_embargo_limitacion IS NOT NULL AND historial_consultas.tiene_embargo_limitacion != '' AND historial_consultas.tiene_embargo_limitacion != 'NO'
                    THEN historial_consultas.tiene_embargo_limitacion
                    ELSE excluded.tiene_embargo_limitacion
                END,
                detalle_limitacion=CASE
                    WHEN historial_consultas.detalle_limitacion IS NOT NULL AND historial_consultas.detalle_limitacion != '' AND historial_consultas.detalle_limitacion != 'NINGUNO'
                    THEN historial_consultas.detalle_limitacion
                    ELSE excluded.detalle_limitacion
                END,
                capturas_guardadas=excluded.capturas_guardadas,
                ruta_expediente=excluded.ruta_expediente
        """, batch_rows)
        conn.commit()
        conn.close()
    except Exception:
        pass

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


def sync_scope_to_sqlite(scope="PATIO", patio=None, anio=None, log_cb=None):
    """
    Sincroniza los expedientes físicos hacia SQLite según el alcance elegido:
    - 'ANIO': Solo el Patio y Año actuales
    - 'PATIO': Todos los años del Patio seleccionado
    - 'TODOS': Todos los Patios y todos los Años
    """
    init_db()
    if scope == "ANIO" and patio and anio:
        if log_cb:
            log_cb(f"Sincronizando BD SQLite: {patio} -> {anio}...")
        scan_patio_anio(patio, anio)
    elif scope == "PATIO" and patio:
        for a in list_anios(patio):
            if log_cb:
                log_cb(f"Sincronizando BD SQLite: {patio} -> {a}...")
            scan_patio_anio(patio, a)
    elif scope == "TODOS":
        for p in list_patios():
            for a in list_anios(p):
                if log_cb:
                    log_cb(f"Sincronizando BD SQLite: {p} -> {a}...")
                scan_patio_anio(p, a)


def generate_sqlite_excel_report(output_path=None, scope="PATIO", patio=None, anio=None, log_cb=None):
    """
    Extrae toda la información directamente desde las tablas de la base de datos SQLite (progreso_consultas.db)
    y construye un archivo Excel profesional (.xlsx) con 4 hojas:
      1. RESUMEN EJECUTIVO BD (KPIs globales y desglose por Patio y Año desde SQLite)
      2. BASE DE DATOS VEHICULOS (Todos los registros de historial_consultas con colores semáforo)
      3. ALERTAS Y EMBARGOS (Registros con Embargo, Fiscalía, Prenda o Inconsistencia de Propietario)
      4. AUDITORIA ACCESOS Y TOKENS (Sesiones y eventos registrados en SQLite)
    Retorna: (ruta_archivo_generado, resumen_dict)
    """
    sync_scope_to_sqlite(scope=scope, patio=patio, anio=anio, log_cb=log_cb)

    os.makedirs(REPORTS_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    if not output_path:
        suffix = scope
        if scope == "PATIO" and patio:
            suffix = patio.replace(" ", "_")
        elif scope == "ANIO" and patio and anio:
            suffix = f"{patio.replace(' ', '_')}_{anio.replace(' ', '_')}"
        output_path = os.path.join(REPORTS_DIR, f"REPORTE_RUNT_SQLITE_{suffix}_{ts}.xlsx")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Filtros SQL según alcance
    where_clause = ""
    params = []
    if scope == "ANIO" and patio and anio:
        where_clause = "WHERE patio = ? AND anio = ?"
        params = [patio, anio]
    elif scope == "PATIO" and patio:
        where_clause = "WHERE patio = ?"
        params = [patio]

    # 1. Consultar Resumen Agrupado por Patio y Año desde SQLite
    cur.execute(f"""
        SELECT
            patio,
            anio,
            COUNT(*) AS total_vehiculos,
            SUM(CASE WHEN estado_consulta = 'COMPLETADO_CIUDADANO' THEN 1 ELSE 0 END) AS completados,
            SUM(CASE WHEN estado_consulta = 'PENDIENTE' THEN 1 ELSE 0 END) AS pendientes,
            SUM(CASE WHEN estado_consulta = 'INCONSISTENCIA_PROPIETARIO' THEN 1 ELSE 0 END) AS inconsistencias,
            SUM(CASE WHEN estado_consulta = 'SIN_PROPIETARIO_CONSTANCIA' THEN 1 ELSE 0 END) AS sin_propietario,
            SUM(CASE WHEN UPPER(COALESCE(tiene_embargo_limitacion, 'NO')) != 'NO'
                       OR UPPER(COALESCE(detalle_limitacion, '')) LIKE '%EMBARGO%'
                       OR UPPER(COALESCE(detalle_limitacion, '')) LIKE '%HURTO%'
                     THEN 1 ELSE 0 END) AS con_embargo_alerta,
            SUM(COALESCE(capturas_guardadas, 0)) AS total_capturas
        FROM historial_consultas
        {where_clause}
        GROUP BY patio, anio
        ORDER BY patio ASC, anio ASC
    """, params)
    resumen_rows = cur.fetchall()

    # 2. Consultar todos los vehículos desde SQLite
    cur.execute(f"""
        SELECT
            id, patio, anio, placa, clase, marca, linea, modelo,
            tipo_doc, nro_doc, propietario,
            estado_consulta, tiene_embargo_limitacion, detalle_limitacion,
            soat_estado, rtm_estado, capturas_guardadas, ruta_expediente, fecha_actualizacion
        FROM historial_consultas
        {where_clause}
        ORDER BY patio ASC, anio ASC, placa ASC
    """, params)
    vehiculos_rows = cur.fetchall()

    # 3. Consultar Sesiones y Auditoría desde SQLite
    sesiones_rows = []
    bitacora_rows = []
    try:
        cur.execute("""
            SELECT id, username, substr(token, 1, 14) || '...', fecha_inicio, fecha_expiracion,
                   CASE WHEN activa = 1 THEN 'ACTIVA' ELSE 'CERRADA / EXPIRADA' END,
                   COALESCE(motivo_cierre, '-')
            FROM sesiones_token
            ORDER BY id DESC LIMIT 500
        """)
        sesiones_rows = cur.fetchall()
    except Exception:
        pass

    try:
        cur.execute("""
            SELECT id, username, evento, detalle, fecha_evento
            FROM bitacora_accesos
            ORDER BY id DESC LIMIT 1000
        """)
        bitacora_rows = cur.fetchall()
    except Exception:
        pass

    conn.close()

    # Construcción del libro Excel con estilos profesionales
    wb = openpyxl.Workbook()
    ws_res = wb.active
    ws_res.title = "RESUMEN EJECUTIVO BD"
    ws_veh = wb.create_sheet("BASE DE DATOS VEHICULOS")
    ws_ale = wb.create_sheet("ALERTAS Y EMBARGOS")
    ws_aud = wb.create_sheet("AUDITORIA SESIONES SQLITE")

    # Paleta de estilos
    header_fill = PatternFill("solid", fgColor="0F172A")
    sub_fill = PatternFill("solid", fgColor="1E3A8A")
    total_fill = PatternFill("solid", fgColor="E2E8F0")
    header_font = Font(name="Segoe UI", size=10, bold=True, color="FFFFFF")
    title_font = Font(name="Segoe UI", size=14, bold=True, color="0F172A")
    bold_font = Font(name="Segoe UI", size=10, bold=True, color="0F172A")
    reg_font = Font(name="Segoe UI", size=10, color="0F172A")

    fill_ok = PatternFill("solid", fgColor="DCFCE7")
    fill_emb = PatternFill("solid", fgColor="FEE2E2")
    fill_inc = PatternFill("solid", fgColor="FFEDD5")
    fill_pen = PatternFill("solid", fgColor="FEF9C3")
    fill_sin = PatternFill("solid", fgColor="F1F5F9")

    thin = Side(border_style="thin", color="CBD5E1")
    border_cell = Border(left=thin, right=thin, top=thin, bottom=thin)

    now_human = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # =========================================================================
    # HOJA 1: RESUMEN EJECUTIVO BD
    # =========================================================================
    ws_res["A1"] = "REPORTE CONSOLIDADO DE VALIDACIÓN RUNT CIUDADANO (EXTRAÍDO DE SQLITE)"
    ws_res["A1"].font = title_font
    ws_res["A2"] = f"Fecha de generación: {now_human}  |  Base de Datos: {DB_PATH}  |  Alcance: {scope}"
    ws_res["A2"].font = Font(name="Segoe UI", size=9, italic=True, color="475569")

    res_headers = [
        "PATIO", "AÑO RETENCIÓN", "TOTAL VEHÍCULOS",
        "COMPLETADOS RUNT CIUDADANO", "PENDIENTES CON PROPIETARIO",
        "CON EMBARGO / LIMITACIÓN", "INCONSISTENCIA PROPIETARIO",
        "SIN PROPIETARIO (CONSTANCIA LEY 1730)", "TOTAL CAPTURAS ARCHIVADAS", "% AVANCE"
    ]
    ws_res.append([])
    ws_res.append(res_headers)
    for col_idx in range(1, len(res_headers) + 1):
        c = ws_res.cell(row=4, column=col_idx)
        c.fill = header_fill
        c.font = header_font
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.border = border_cell

    tot_gen = [0, 0, 0, 0, 0, 0, 0]
    row_num = 5
    for r in resumen_rows:
        p_nom, a_nom, t_veh, comp, pend, inc, sin_p, emb, caps = r
        t_veh = t_veh or 0
        comp = comp or 0
        pend = pend or 0
        inc = inc or 0
        sin_p = sin_p or 0
        emb = emb or 0
        caps = caps or 0

        tot_gen[0] += t_veh
        tot_gen[1] += comp
        tot_gen[2] += pend
        tot_gen[3] += emb
        tot_gen[4] += inc
        tot_gen[5] += sin_p
        tot_gen[6] += caps

        procesados = comp + inc + sin_p
        pct = f"{(procesados / t_veh * 100):.1f}%" if t_veh > 0 else "0.0%"

        vals = [p_nom, a_nom, t_veh, comp, pend, emb, inc, sin_p, caps, pct]
        ws_res.append(vals)
        for col_idx in range(1, len(vals) + 1):
            cell = ws_res.cell(row=row_num, column=col_idx)
            cell.font = reg_font
            cell.border = border_cell
            cell.alignment = Alignment(horizontal="center" if col_idx > 2 else "left", vertical="center")
        row_num += 1

    # Fila de Totales
    tot_proc = tot_gen[1] + tot_gen[4] + tot_gen[5]
    pct_tot = f"{(tot_proc / tot_gen[0] * 100):.1f}%" if tot_gen[0] > 0 else "0.0%"
    tot_vals = ["TOTAL GENERAL EN BASE DE DATOS", "-", tot_gen[0], tot_gen[1], tot_gen[2], tot_gen[3], tot_gen[4], tot_gen[5], tot_gen[6], pct_tot]
    ws_res.append(tot_vals)
    for col_idx in range(1, len(tot_vals) + 1):
        cell = ws_res.cell(row=row_num, column=col_idx)
        cell.fill = total_fill
        cell.font = bold_font
        cell.border = border_cell
        cell.alignment = Alignment(horizontal="center" if col_idx > 2 else "left", vertical="center")

    # =========================================================================
    # HOJA 2 Y HOJA 3: BASE DE DATOS VEHÍCULOS Y ALERTAS/EMBARGOS
    # =========================================================================
    veh_headers = [
        "ID BD", "PATIO", "AÑO RETENCIÓN", "PLACA", "CLASE", "MARCA", "LÍNEA", "MODELO",
        "TIPO DOCUMENTO", "NRO DOCUMENTO", "PROPIETARIO REGISTRADO",
        "ESTADO EXPEDIENTE", "¿EMBARGO / LIMITACIÓN?", "DETALLE LIMITACIÓN / NOVEDAD",
        "ESTADO SOAT", "ESTADO RTM", "N° CAPTURAS", "RUTA EXPEDIENTE VEHICULAR", "FECHA ACTUALIZACIÓN BD"
    ]

    for sheet in (ws_veh, ws_ale):
        sheet.append(veh_headers)
        for col_idx in range(1, len(veh_headers) + 1):
            c = sheet.cell(row=1, column=col_idx)
            c.fill = header_fill
            c.font = header_font
            c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            c.border = border_cell

    r_veh_idx = 2
    r_ale_idx = 2
    for row in vehiculos_rows:
        (
            vid, pat, ani, pla, cla, mar, lin, mod,
            tdoc, ndoc, prop, est, temb, dlim, soat, rtm, caps, ruta, f_act
        ) = row

        est_up = str(est or "").upper()
        temb_up = str(temb or "NO").upper()
        dlim_up = str(dlim or "").upper()

        row_fill = fill_pen
        is_alert = False
        if "SI" in temb_up or "EMBARGO" in dlim_up or "HURTO" in dlim_up or "PRENDA" in temb_up or "GARANTIA" in dlim_up:
            row_fill = fill_emb
            is_alert = True
        elif est_up == "COMPLETADO_CIUDADANO":
            row_fill = fill_ok
        elif est_up == "INCONSISTENCIA_PROPIETARIO":
            row_fill = fill_inc
            is_alert = True
        elif est_up == "SIN_PROPIETARIO_CONSTANCIA":
            row_fill = fill_sin

        ws_veh.append(list(row))
        for col_idx in range(1, len(veh_headers) + 1):
            c = ws_veh.cell(row=r_veh_idx, column=col_idx)
            c.fill = row_fill
            c.font = reg_font
            c.border = border_cell
        r_veh_idx += 1

        if is_alert:
            ws_ale.append(list(row))
            for col_idx in range(1, len(veh_headers) + 1):
                c = ws_ale.cell(row=r_ale_idx, column=col_idx)
                c.fill = row_fill
                c.font = reg_font
                c.border = border_cell
            r_ale_idx += 1

    # =========================================================================
    # HOJA 4: AUDITORÍA DE SESIONES Y ACCESOS (SQLITE)
    # =========================================================================
    ws_aud["A1"] = "HISTORIAL DE SESIONES CON TOKEN (VIGENCIA 60 MINUTOS) - SQLITE"
    ws_aud["A1"].font = bold_font
    aud_sess_headers = ["ID SESIÓN", "USUARIO", "TOKEN (PREFIJO)", "FECHA INICIO", "FECHA EXPIRACIÓN (60 MIN)", "ESTADO", "MOTIVO CIERRE"]
    ws_aud.append(aud_sess_headers)
    for col_idx in range(1, len(aud_sess_headers) + 1):
        c = ws_aud.cell(row=2, column=col_idx)
        c.fill = sub_fill
        c.font = header_font
        c.border = border_cell

    curr_aud_row = 3
    for s_row in sesiones_rows:
        ws_aud.append(list(s_row))
        for col_idx in range(1, len(aud_sess_headers) + 1):
            c = ws_aud.cell(row=curr_aud_row, column=col_idx)
            c.font = reg_font
            c.border = border_cell
        curr_aud_row += 1

    curr_aud_row += 2
    ws_aud.cell(row=curr_aud_row, column=1, value="BITÁCORA DE EVENTOS DE SEGURIDAD Y ACCESOS - SQLITE").font = bold_font
    curr_aud_row += 1
    bit_headers = ["ID EVENTO", "USUARIO", "EVENTO", "DETALLE", "FECHA Y HORA"]
    for col_idx, h_text in enumerate(bit_headers, start=1):
        c = ws_aud.cell(row=curr_aud_row, column=col_idx, value=h_text)
        c.fill = header_fill
        c.font = header_font
        c.border = border_cell
    curr_aud_row += 1

    for b_row in bitacora_rows:
        for col_idx, val in enumerate(b_row, start=1):
            c = ws_aud.cell(row=curr_aud_row, column=col_idx, value=val)
            c.font = reg_font
            c.border = border_cell
        curr_aud_row += 1

    # Ajustar ancho de columnas y activar autofiltros
    for ws in (ws_res, ws_veh, ws_ale, ws_aud):
        if ws in (ws_veh, ws_ale) and ws.max_row >= 1:
            ws.auto_filter.ref = f"A1:{get_column_letter(ws.max_column)}{ws.max_row}"
            ws.freeze_panes = "A2"
        elif ws == ws_res and ws.max_row >= 4:
            ws.freeze_panes = "A5"
        for col in ws.columns:
            max_len = 0
            col_letter = get_column_letter(col[0].column)
            for cell in col[:250]:
                val_str = str(cell.value or "")
                if len(val_str) > max_len:
                    max_len = len(val_str)
            ws.column_dimensions[col_letter].width = min(max(max_len + 4, 12), 48)

    wb.save(output_path)

    summary_info = {
        "total_vehiculos": tot_gen[0],
        "completados": tot_gen[1],
        "pendientes": tot_gen[2],
        "embargos": tot_gen[3],
        "inconsistencias": tot_gen[4],
        "sin_propietario": tot_gen[5],
        "total_capturas": tot_gen[6],
        "output_path": output_path
    }
    return output_path, summary_info


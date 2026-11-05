# -*- coding: utf-8 -*-
"""
Panel de Control Gráfico (GUI): Automatizador RUNT Ciudadano + Sistema de Autenticación SQLite (Token 60 Minutos).
Características de Seguridad e Integración:
1. Al iniciar o cerrar el programa, invalida cualquier sesión anterior en SQLite (siempre exige iniciar sesión).
2. Login con validación de credenciales cifradas (PBKDF2-HMAC-SHA256), control de intentos fallidos y generación de Token.
3. Token de sesión con expiración estricta de 60 minutos (cuenta regresiva en vivo en la barra superior).
4. Si el token llega a 00:00 o el usuario presiona "Cerrar Sesión", pausa el lote, revoca el token en SQLite
   y bloquea el panel regresando a la pantalla de inicio de sesión.
5. Gestión de usuarios integrada (Crear / Cambiar contraseña en SQLite).
"""

import os
import io
import queue
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox
from PIL import Image, ImageTk

import auth_manager
import scanner_patios
from bot_playwright_runt import RuntCiudadanoBot


class AutomatizadorRuntGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Automatizador Oficial de Expedientes RUNT Ciudadano - Acceso Seguro")
        self.root.geometry("1280x800")
        self.root.configure(bg="#0f172a")

        # Al abrir el programa, invalidar todas las sesiones previas para exigir siempre login nuevo
        auth_manager.init_auth_db()
        auth_manager.invalidate_all_active_sessions("INICIO_APLICACION_NUEVA_SESION_REQUERIDA")

        # Estado de sesión actual
        self.session = None
        self.timer_job = None

        # Datos del escáner y lote
        self.vehicles = []
        self.stats = {}
        self.current_index = None

        self.cmd_queue = queue.Queue()
        self.is_running_batch = False
        self.is_paused = False
        self.captcha_photo = None

        # Contenedores principales (Login vs Dashboard)
        self.login_container = None
        self.dashboard_container = None

        self._setup_styles()

        # Iniciar hilo trabajador de Playwright en segundo plano
        self.worker_thread = threading.Thread(target=self._playwright_worker_loop, daemon=True)
        self.worker_thread.start()

        # Al cerrar la ventana con la X, revocar el token activo en SQLite
        self.root.protocol("WM_DELETE_WINDOW", self._on_close_window)

        # Mostrar pantalla de inicio de sesión
        self._show_login_view()

    def _setup_styles(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview",
                        background="#1e293b",
                        foreground="#f8fafc",
                        rowheight=28,
                        fieldbackground="#1e293b",
                        font=("Segoe UI", 10))
        style.configure("Treeview.Heading",
                        background="#0f172a",
                        foreground="#38bdf8",
                        font=("Segoe UI", 10, "bold"))
        style.map("Treeview", background=[("selected", "#2563eb")])

    # =========================================================================
    # 1. PANTALLA DE INICIO DE SESIÓN (LOGIN) Y GESTIÓN DE TOKEN (60 MINUTOS)
    # =========================================================================
    def _show_login_view(self, info_msg=None):
        if self.timer_job:
            self.root.after_cancel(self.timer_job)
            self.timer_job = None

        if self.dashboard_container:
            self.dashboard_container.destroy()
            self.dashboard_container = None

        if self.login_container:
            self.login_container.destroy()

        self.root.title("Iniciar Sesión - Automatizador Oficial RUNT Ciudadano")

        self.login_container = tk.Frame(self.root, bg="#0f172a")
        self.login_container.pack(fill=tk.BOTH, expand=True)

        # Tarjeta central de Login
        card = tk.Frame(
            self.login_container, bg="#1e293b",
            padx=38, pady=34,
            highlightbackground="#38bdf8", highlightthickness=1
        )
        card.place(relx=0.5, rely=0.5, anchor="center", width=460)

        tk.Label(
            card, text="🔐 ACCESO AL SISTEMA RUNT",
            bg="#1e293b", fg="#38bdf8",
            font=("Segoe UI", 16, "bold")
        ).pack(pady=(0, 4))

        tk.Label(
            card,
            text="Validación y Captura Automatizada de Expedientes Vehiculares\nSesión protegida con Token SQLite (Vigencia: 60 minutos)",
            bg="#1e293b", fg="#94a3b8",
            font=("Segoe UI", 9), justify="center"
        ).pack(pady=(0, 18))

        if info_msg:
            lbl_alert = tk.Label(
                card, text=info_msg,
                bg="#451a1c", fg="#fca5a5",
                font=("Segoe UI", 9, "bold"),
                padx=10, pady=8, wraplength=360, justify="center"
            )
            lbl_alert.pack(fill=tk.X, pady=(0, 14))

        # Usuario
        tk.Label(
            card, text="Usuario:", bg="#1e293b", fg="#e2e8f0",
            font=("Segoe UI", 10, "bold")
        ).pack(anchor="w")
        self.ent_login_user = tk.Entry(
            card, font=("Segoe UI", 12),
            bg="#0f172a", fg="#f8fafc", insertbackground="#ffffff",
            relief=tk.FLAT, highlightthickness=1, highlightbackground="#334155"
        )
        self.ent_login_user.pack(fill=tk.X, ipady=6, pady=(4, 14))
        self.ent_login_user.bind("<Return>", lambda e: self.ent_login_pwd.focus_set())

        # Contraseña
        tk.Label(
            card, text="Contraseña:", bg="#1e293b", fg="#e2e8f0",
            font=("Segoe UI", 10, "bold")
        ).pack(anchor="w")

        pwd_frame = tk.Frame(card, bg="#0f172a", highlightthickness=1, highlightbackground="#334155")
        pwd_frame.pack(fill=tk.X, pady=(4, 8))

        self.show_pwd_var = tk.BooleanVar(value=False)
        self.ent_login_pwd = tk.Entry(
            pwd_frame, font=("Segoe UI", 12), show="•",
            bg="#0f172a", fg="#f8fafc", insertbackground="#ffffff",
            relief=tk.FLAT
        )
        self.ent_login_pwd.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=6, padx=6)
        self.ent_login_pwd.bind("<Return>", lambda e: self._attempt_login())

        btn_eye = tk.Checkbutton(
            pwd_frame, text="Ver", variable=self.show_pwd_var,
            bg="#0f172a", fg="#94a3b8", selectcolor="#0f172a",
            activebackground="#0f172a", activeforeground="#38bdf8",
            font=("Segoe UI", 8),
            command=lambda: self.ent_login_pwd.configure(show="" if self.show_pwd_var.get() else "•")
        )
        btn_eye.pack(side=tk.RIGHT, padx=6)

        self.lbl_login_status = tk.Label(
            card, text="", bg="#1e293b", fg="#f87171",
            font=("Segoe UI", 9, "bold"), wraplength=360
        )
        self.lbl_login_status.pack(fill=tk.X, pady=(4, 8))

        btn_login = tk.Button(
            card, text="INGRESAR AL SISTEMA ➔",
            bg="#0284c7", fg="#ffffff",
            font=("Segoe UI", 11, "bold"),
            relief=tk.FLAT, pady=10, cursor="hand2",
            command=self._attempt_login
        )
        btn_login.pack(fill=tk.X, pady=(6, 4))

        self.ent_login_user.focus_set()

    def _attempt_login(self):
        user = self.ent_login_user.get().strip()
        pwd = self.ent_login_pwd.get()

        ok, msg, session_data = auth_manager.authenticate_user(user, pwd)
        if not ok:
            self.lbl_login_status.configure(text=f"⚠ {msg}", fg="#f87171")
            return

        self.session = session_data
        self._show_dashboard_view()

    def _start_session_countdown(self):
        """
        Verifica cada segundo en SQLite que el token siga activo y actualiza el reloj de 60:00 a 00:00.
        Si expira, detiene cualquier lote y bloquea el sistema enviando al Login.
        """
        if not self.session:
            return

        token = self.session.get("token")
        valid, rem_secs, msg = auth_manager.validate_session_token(token)

        if not valid or rem_secs <= 0:
            self._force_logout_due_to_expiration(msg)
            return

        mins = rem_secs // 60
        secs = rem_secs % 60
        token_short = token[:10] + "..."

        color_timer = "#4ade80" if mins >= 10 else ("#facc15" if mins >= 3 else "#f87171")
        if hasattr(self, "lbl_session_timer") and self.lbl_session_timer.winfo_exists():
            self.lbl_session_timer.configure(
                text=f"🔑 Token: {token_short}  |  ⏱ Expira en: {mins:02d}:{secs:02d} min",
                fg=color_timer
            )

        self.timer_job = self.root.after(1000, self._start_session_countdown)

    def _force_logout_due_to_expiration(self, reason_msg):
        self.is_paused = True
        self.is_running_batch = False
        if self.session:
            auth_manager.logout_session(self.session.get("token"), "EXPIRACION_AUTOMATICA_60_MIN")
        self.session = None
        messagebox.showwarning(
            "Sesión Expirada (60 Minutos)",
            f"{reason_msg}\n\nEl proceso en curso se ha pausado de forma segura."
        )
        self._show_login_view(info_msg="Tu sesión anterior cumplió los 60 minutos de validez. Inicia sesión nuevamente para continuar.")

    def _manual_logout(self):
        if not self.session:
            return
        if not messagebox.askyesno("Cerrar Sesión", "¿Deseas cerrar tu sesión actual?\nSi hay un lote en ejecución se pausará inmediatamente."):
            return
        self.is_paused = True
        self.is_running_batch = False
        auth_manager.logout_session(self.session.get("token"), "CIERRE_SESION_MANUAL_USUARIO")
        self.session = None
        self._show_login_view(info_msg="Has cerrado sesión correctamente. Tu token fue invalidado en la base de datos.")

    def _open_user_manager_dialog(self):
        if not self.session:
            return
        dlg = tk.Toplevel(self.root)
        dlg.title("Gestión de Usuarios y Contraseñas (SQLite)")
        dlg.geometry("420x350")
        dlg.configure(bg="#1e293b")
        dlg.transient(self.root)
        dlg.grab_set()

        frame = tk.Frame(dlg, bg="#1e293b", padx=20, pady=18)
        frame.pack(fill=tk.BOTH, expand=True)

        tk.Label(frame, text="Crear Usuario o Cambiar Contraseña", bg="#1e293b", fg="#38bdf8", font=("Segoe UI", 12, "bold")).pack(anchor="w", pady=(0, 12))

        tk.Label(frame, text="Nombre de usuario (login):", bg="#1e293b", fg="#cbd5e1", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        ent_u = tk.Entry(frame, font=("Segoe UI", 10), bg="#0f172a", fg="#ffffff", insertbackground="#ffffff")
        ent_u.pack(fill=tk.X, ipady=4, pady=(2, 8))
        ent_u.insert(0, self.session["username"])

        tk.Label(frame, text="Nombre completo:", bg="#1e293b", fg="#cbd5e1", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        ent_n = tk.Entry(frame, font=("Segoe UI", 10), bg="#0f172a", fg="#ffffff", insertbackground="#ffffff")
        ent_n.pack(fill=tk.X, ipady=4, pady=(2, 8))
        ent_n.insert(0, self.session["nombre_completo"])

        tk.Label(frame, text="Nueva contraseña (mínimo 6 caracteres):", bg="#1e293b", fg="#cbd5e1", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        ent_p = tk.Entry(frame, font=("Segoe UI", 10), show="•", bg="#0f172a", fg="#ffffff", insertbackground="#ffffff")
        ent_p.pack(fill=tk.X, ipady=4, pady=(2, 14))

        def _save():
            ok, msg = auth_manager.create_or_update_user(ent_u.get(), ent_n.get(), ent_p.get(), "ADMIN")
            if ok:
                messagebox.showinfo("Usuario Guardado", msg, parent=dlg)
                dlg.destroy()
            else:
                messagebox.showerror("Error de Validación", msg, parent=dlg)

        tk.Button(
            frame, text="💾 Guardar en SQLite", bg="#16a34a", fg="#ffffff",
            font=("Segoe UI", 10, "bold"), relief=tk.FLAT, pady=8, cursor="hand2",
            command=_save
        ).pack(fill=tk.X)

    # =========================================================================
    # 2. PANEL PRINCIPAL DE CONTROL DE PATIOS (DASHBOARD AUTENTICADO)
    # =========================================================================
    def _show_dashboard_view(self):
        if self.login_container:
            self.login_container.destroy()
            self.login_container = None

        self.root.title(
            f"Automatizador Oficial RUNT Ciudadano - Sesión Activa: {self.session['nombre_completo']} ({self.session['username']})"
        )

        self.dashboard_container = tk.Frame(self.root, bg="#0f172a")
        self.dashboard_container.pack(fill=tk.BOTH, expand=True)

        # Barra superior de Sesión y Seguridad (Token + Cuenta regresiva 60 min + Cerrar Sesión)
        session_bar = tk.Frame(self.dashboard_container, bg="#090d16", padx=14, pady=6)
        session_bar.pack(fill=tk.X)

        tk.Label(
            session_bar,
            text=f"👤 Usuario Activo: {self.session['nombre_completo']} ({self.session['username']})",
            bg="#090d16", fg="#e2e8f0", font=("Segoe UI", 9, "bold")
        ).pack(side=tk.LEFT)

        btn_logout = tk.Button(
            session_bar, text="🚪 Cerrar Sesión",
            bg="#dc2626", fg="#ffffff", font=("Segoe UI", 9, "bold"),
            relief=tk.FLAT, padx=10, pady=2, cursor="hand2",
            command=self._manual_logout
        )
        btn_logout.pack(side=tk.RIGHT, padx=(8, 0))

        btn_users = tk.Button(
            session_bar, text="⚙ Usuarios / Clave",
            bg="#334155", fg="#f8fafc", font=("Segoe UI", 9),
            relief=tk.FLAT, padx=10, pady=2, cursor="hand2",
            command=self._open_user_manager_dialog
        )
        btn_users.pack(side=tk.RIGHT, padx=6)

        self.lbl_session_timer = tk.Label(
            session_bar,
            text="🔑 Token activo | ⏱ Expira en: 60:00 min",
            bg="#090d16", fg="#4ade80", font=("Consolas", 10, "bold")
        )
        self.lbl_session_timer.pack(side=tk.RIGHT, padx=14)

        # 1. Barra de título y selectores de Patio / Año
        top_frame = tk.Frame(self.dashboard_container, bg="#1e293b", padx=16, pady=10)
        top_frame.pack(fill=tk.X, padx=12, pady=(8, 6))

        tk.Label(
            top_frame,
            text="AUTOMATIZADOR DE EXPEDIENTES RUNT CIUDADANO",
            bg="#1e293b", fg="#f8fafc",
            font=("Segoe UI", 14, "bold")
        ).pack(side=tk.LEFT, padx=(0, 20))

        tk.Label(top_frame, text="Patio:", bg="#1e293b", fg="#cbd5e1", font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT)
        self.cmb_patio = ttk.Combobox(top_frame, state="readonly", width=30, font=("Segoe UI", 10))
        self.cmb_patio.pack(side=tk.LEFT, padx=(6, 14))
        self.cmb_patio.bind("<<ComboboxSelected>>", self._on_patio_selected)

        tk.Label(top_frame, text="Año Retención:", bg="#1e293b", fg="#cbd5e1", font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT)
        self.cmb_anio = ttk.Combobox(top_frame, state="readonly", width=14, font=("Segoe UI", 10))
        self.cmb_anio.pack(side=tk.LEFT, padx=(6, 14))
        self.cmb_anio.bind("<<ComboboxSelected>>", self._on_anio_selected)

        btn_refresh = tk.Button(
            top_frame, text="🔄 Actualizar Escaneo",
            bg="#334155", fg="#f8fafc", font=("Segoe UI", 10, "bold"),
            relief=tk.FLAT, padx=12, pady=4, cursor="hand2",
            command=self._scan_current_selection
        )
        btn_refresh.pack(side=tk.LEFT, padx=6)

        # 2. Panel de tarjetas de indicadores (KPIs)
        kpi_frame = tk.Frame(self.dashboard_container, bg="#0f172a")
        kpi_frame.pack(fill=tk.X, padx=12, pady=4)

        self.kpi_vars = {
            "total": tk.StringVar(value="0"),
            "pendientes": tk.StringVar(value="0"),
            "completados": tk.StringVar(value="0"),
            "embargos": tk.StringVar(value="0"),
            "inconsistencias": tk.StringVar(value="0"),
            "sin_prop": tk.StringVar(value="0")
        }

        kpi_specs = [
            ("Total en Año", "total", "#38bdf8"),
            ("Pendientes por Consultar", "pendientes", "#facc15"),
            ("Completados RUNT Ciudadano", "completados", "#4ade80"),
            ("Con Embargo / Fiscalía", "embargos", "#f87171"),
            ("Inconsistencia Propietario", "inconsistencias", "#fb923c"),
            ("Cerrados Sin Propietario (Ley 1730)", "sin_prop", "#94a3b8")
        ]

        for label_txt, key, color in kpi_specs:
            card = tk.Frame(kpi_frame, bg="#1e293b", padx=14, pady=8, highlightbackground="#334155", highlightthickness=1)
            card.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=4)
            tk.Label(card, textvariable=self.kpi_vars[key], bg="#1e293b", fg=color, font=("Segoe UI", 19, "bold")).pack(anchor="w")
            tk.Label(card, text=label_txt, bg="#1e293b", fg="#cbd5e1", font=("Segoe UI", 9)).pack(anchor="w")

        # 3. Panel Central Dividido: Izquierda (Tabla) | Derecha (Consola + Captcha Turbo)
        mid_frame = tk.Frame(self.dashboard_container, bg="#0f172a")
        mid_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=6)

        left_frame = tk.Frame(mid_frame, bg="#1e293b", padx=8, pady=8)
        left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 8))

        filter_bar = tk.Frame(left_frame, bg="#1e293b")
        filter_bar.pack(fill=tk.X, pady=(0, 6))
        tk.Label(filter_bar, text="Filtrar vista:", bg="#1e293b", fg="#cbd5e1", font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT)

        self.filter_var = tk.StringVar(value="PENDIENTES_Y_RECIENTES")
        for txt, val in [
            ("Pendientes + Consultados", "PENDIENTES_Y_RECIENTES"),
            ("Solo Pendientes", "SOLO_PENDIENTES"),
            ("Solo Con Embargo / Alerta", "SOLO_EMBARGOS"),
            ("Todos (Incluye Sin Propietario)", "TODOS")
        ]:
            tk.Radiobutton(
                filter_bar, text=txt, variable=self.filter_var, value=val,
                bg="#1e293b", fg="#f8fafc", selectcolor="#0f172a",
                activebackground="#1e293b", activeforeground="#38bdf8",
                font=("Segoe UI", 9), command=self._populate_tree
            ).pack(side=tk.LEFT, padx=8)

        cols = ("placa", "tipo_doc", "nro_doc", "propietario", "estado", "embargo", "imgs")
        self.tree = ttk.Treeview(left_frame, columns=cols, show="headings", selectmode="browse")
        self.tree.heading("placa", text="PLACA")
        self.tree.heading("tipo_doc", text="TIPO DOC")
        self.tree.heading("nro_doc", text="DOCUMENTO")
        self.tree.heading("propietario", text="PROPIETARIO REGISTRADO")
        self.tree.heading("estado", text="ESTADO EXPEDIENTE")
        self.tree.heading("embargo", text="EMBARGO / LIMITACIÓN")
        self.tree.heading("imgs", text="CAPTURAS")

        self.tree.column("placa", width=80, anchor="center")
        self.tree.column("tipo_doc", width=120, anchor="center")
        self.tree.column("nro_doc", width=110, anchor="center")
        self.tree.column("propietario", width=230, anchor="w")
        self.tree.column("estado", width=155, anchor="center")
        self.tree.column("embargo", width=165, anchor="w")
        self.tree.column("imgs", width=75, anchor="center")

        self.tree.tag_configure("PENDIENTE", foreground="#fde047")
        self.tree.tag_configure("COMPLETADO_CIUDADANO", foreground="#4ade80")
        self.tree.tag_configure("EMBARGO", foreground="#f87171", background="#3b181e")
        self.tree.tag_configure("INCONSISTENCIA_PROPIETARIO", foreground="#fb923c")
        self.tree.tag_configure("SIN_PROPIETARIO_CONSTANCIA", foreground="#94a3b8")

        vsb = ttk.Scrollbar(left_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        # Panel Derecho
        right_frame = tk.Frame(mid_frame, bg="#1e293b", width=390, padx=14, pady=12)
        right_frame.pack(side=tk.RIGHT, fill=tk.Y)
        right_frame.pack_propagate(False)

        tk.Label(
            right_frame, text="PANEL DE EJECUCIÓN Y CAPTCHA TURBO",
            bg="#1e293b", fg="#38bdf8", font=("Segoe UI", 11, "bold")
        ).pack(anchor="w", pady=(0, 8))

        self.lbl_active_veh = tk.Label(
            right_frame,
            text="Vehículo en turno: Ninguno",
            bg="#0f172a", fg="#f8fafc", font=("Segoe UI", 10, "bold"),
            padx=10, pady=8, anchor="w", justify="left", wraplength=350
        )
        self.lbl_active_veh.pack(fill=tk.X, pady=(0, 10))

        btn_grid = tk.Frame(right_frame, bg="#1e293b")
        btn_grid.pack(fill=tk.X, pady=(0, 10))

        self.btn_start = tk.Button(
            btn_grid, text="▶ INICIAR LOTE AUTOMÁTICO",
            bg="#16a34a", fg="#ffffff", font=("Segoe UI", 10, "bold"),
            relief=tk.FLAT, pady=8, cursor="hand2",
            command=self._start_batch_process
        )
        self.btn_start.pack(fill=tk.X, pady=3)

        self.btn_single = tk.Button(
            btn_grid, text="🎯 Consultar Solo Placa Seleccionada",
            bg="#2563eb", fg="#ffffff", font=("Segoe UI", 10, "bold"),
            relief=tk.FLAT, pady=6, cursor="hand2",
            command=self._run_selected_single
        )
        self.btn_single.pack(fill=tk.X, pady=3)

        row_pause = tk.Frame(btn_grid, bg="#1e293b")
        row_pause.pack(fill=tk.X, pady=3)

        self.btn_pause = tk.Button(
            row_pause, text="⏸ Pausar Lote",
            bg="#d97706", fg="#ffffff", font=("Segoe UI", 9, "bold"),
            relief=tk.FLAT, pady=5, cursor="hand2",
            command=self._toggle_pause
        )
        self.btn_pause.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 4))

        self.btn_open_folder = tk.Button(
            row_pause, text="📂 Abrir EXPEDIENTE VEHICULAR",
            bg="#475569", fg="#ffffff", font=("Segoe UI", 9, "bold"),
            relief=tk.FLAT, pady=5, cursor="hand2",
            command=self._open_selected_expediente
        )
        self.btn_open_folder.pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=(4, 0))

        cap_box = tk.Frame(right_frame, bg="#0f172a", padx=10, pady=10, highlightbackground="#38bdf8", highlightthickness=1)
        cap_box.pack(fill=tk.X, pady=(6, 10))

        tk.Label(
            cap_box,
            text="VISTA RÁPIDA DE CAPTCHA (Opcional: digita aquí + Enter\no escríbelo directo en la ventana de Chrome):",
            bg="#0f172a", fg="#cbd5e1", font=("Segoe UI", 8), justify="left"
        ).pack(anchor="w", pady=(0, 6))

        self.lbl_captcha_img = tk.Label(
            cap_box, text="[El Captcha aparecerá aquí al cargar la placa]",
            bg="#ffffff", fg="#475569", height=3, font=("Segoe UI", 9)
        )
        self.lbl_captcha_img.pack(fill=tk.X, pady=(0, 8))

        entry_row = tk.Frame(cap_box, bg="#0f172a")
        entry_row.pack(fill=tk.X)

        self.ent_captcha = tk.Entry(
            entry_row, font=("Consolas", 15, "bold"),
            bg="#1e293b", fg="#38bdf8", insertbackground="#ffffff", width=12, justify="center"
        )
        self.ent_captcha.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        self.ent_captcha.bind("<Return>", lambda e: self._send_captcha_from_entry())

        tk.Button(
            entry_row, text="Enviar ↵",
            bg="#0284c7", fg="#ffffff", font=("Segoe UI", 10, "bold"),
            relief=tk.FLAT, padx=12, cursor="hand2",
            command=self._send_captcha_from_entry
        ).pack(side=tk.RIGHT)

        retry_box = tk.Frame(right_frame, bg="#1e293b")
        retry_box.pack(fill=tk.X, pady=(2, 8))
        tk.Label(retry_box, text="Cambiar Tipo Doc si falla:", bg="#1e293b", fg="#94a3b8", font=("Segoe UI", 8)).pack(side=tk.LEFT)
        for doc_opt, short_lbl in [("Cédula de Extranjería", "Probar C.E."), ("NIT", "Probar NIT"), ("Pasaporte", "Probar PA")]:
            tk.Button(
                retry_box, text=short_lbl, bg="#334155", fg="#e2e8f0",
                font=("Segoe UI", 8), relief=tk.FLAT, padx=6, pady=2,
                command=lambda d=doc_opt: self._retry_with_doc_type(d)
            ).pack(side=tk.LEFT, padx=3)

        tk.Label(right_frame, text="Bitácora de Ejecución en Vivo:", bg="#1e293b", fg="#cbd5e1", font=("Segoe UI", 9, "bold")).pack(anchor="w")
        self.txt_log = tk.Text(
            right_frame, bg="#0f172a", fg="#e2e8f0",
            font=("Consolas", 9), height=12, state="disabled", wrap="word"
        )
        self.txt_log.pack(fill=tk.BOTH, expand=True, pady=(4, 0))

        # Iniciar contador regresivo del Token de 60 minutos y cargar los patios
        self._start_session_countdown()
        self._load_patios()

    # =========================================================================
    # 3. VALIDACIÓN DE TOKEN ANTES DE CADA ACCIÓN Y LÓGICA OPERATIVA
    # =========================================================================
    def _ensure_authenticated(self) -> bool:
        if not self.session:
            return False
        valid, rem, msg = auth_manager.validate_session_token(self.session.get("token"))
        if not valid or rem <= 0:
            self.root.after(0, lambda: self._force_logout_due_to_expiration(msg))
            return False
        return True

    def log_message(self, msg):
        def _append():
            if hasattr(self, "txt_log") and self.txt_log.winfo_exists():
                self.txt_log.configure(state="normal")
                self.txt_log.insert(tk.END, f"• {msg}\n")
                self.txt_log.see(tk.END)
                self.txt_log.configure(state="disabled")
        self.root.after(0, _append)

    def _update_captcha_preview(self, png_bytes):
        def _draw():
            if not hasattr(self, "lbl_captcha_img") or not self.lbl_captcha_img.winfo_exists():
                return
            if not png_bytes:
                self.lbl_captcha_img.configure(image="", text="[Mira el Captcha en Chrome]")
                return
            try:
                pil_img = Image.open(io.BytesIO(png_bytes))
                pil_img = pil_img.resize((240, 62), Image.Resampling.LANCZOS)
                self.captcha_photo = ImageTk.PhotoImage(pil_img)
                self.lbl_captcha_img.configure(image=self.captcha_photo, text="", height=62)
                self.ent_captcha.delete(0, tk.END)
                self.ent_captcha.focus_set()
            except Exception:
                pass
        self.root.after(0, _draw)

    def _load_patios(self):
        if not self._ensure_authenticated():
            return
        patios = scanner_patios.list_patios()
        self.cmb_patio["values"] = patios
        if patios:
            default_p = next((p for p in patios if "FABRICATO" in p.upper()), patios[0])
            self.cmb_patio.set(default_p)
            self._on_patio_selected()

    def _on_patio_selected(self, event=None):
        if not self._ensure_authenticated():
            return
        patio = self.cmb_patio.get()
        anios = scanner_patios.list_anios(patio)
        self.cmb_anio["values"] = anios
        if anios:
            default_a = next((a for a in anios if "2024" in a), anios[0])
            self.cmb_anio.set(default_a)
            self._scan_current_selection()

    def _on_anio_selected(self, event=None):
        if not self._ensure_authenticated():
            return
        self._scan_current_selection()

    def _scan_current_selection(self):
        if not self._ensure_authenticated():
            return
        if not hasattr(self, "cmb_patio") or not self.cmb_patio.winfo_exists():
            return
        patio = self.cmb_patio.get()
        anio = self.cmb_anio.get()
        if not patio or not anio:
            return
        self.log_message(f"Escaneando {patio} -> {anio}...")
        res = scanner_patios.scan_patio_anio(patio, anio)
        self.vehicles = res["vehicles"]
        self.stats = res["stats"]

        self.kpi_vars["total"].set(str(self.stats.get("total", 0)))
        self.kpi_vars["pendientes"].set(str(self.stats.get("pendientes_con_propietario", 0)))
        self.kpi_vars["completados"].set(str(self.stats.get("completados_ciudadano", 0)))
        self.kpi_vars["embargos"].set(str(self.stats.get("con_embargo_alerta", 0)))
        self.kpi_vars["inconsistencias"].set(str(self.stats.get("inconsistencias", 0)))
        self.kpi_vars["sin_prop"].set(str(self.stats.get("sin_propietario_constancia", 0)))

        self._populate_tree()
        self.log_message(
            f"Escaneo listo: {self.stats.get('pendientes_con_propietario', 0)} pendientes con propietario | "
            f"{self.stats.get('completados_ciudadano', 0)} completados | "
            f"{self.stats.get('sin_propietario_constancia', 0)} cerrados sin propietario."
        )

    def _populate_tree(self):
        if not hasattr(self, "tree") or not self.tree.winfo_exists():
            return
        for item in self.tree.get_children():
            self.tree.delete(item)

        mode = self.filter_var.get()
        for idx, v in enumerate(self.vehicles):
            est = v["estado"]
            emb = str(v.get("tiene_embargo") or "NO")
            det = str(v.get("detalle_limitacion") or "")

            if mode == "SOLO_PENDIENTES" and est != "PENDIENTE":
                continue
            if mode == "PENDIENTES_Y_RECIENTES" and est == "SIN_PROPIETARIO_CONSTANCIA":
                continue
            if mode == "SOLO_EMBARGOS" and ("SI" not in emb.upper() and "PRENDA" not in emb.upper()):
                continue

            tag = est
            if "SI" in emb.upper() or "EMBARGO" in det.upper() or "HURTO" in det.upper():
                tag = "EMBARGO"

            emb_display = det if det and det != "NINGUNO" else emb
            self.tree.insert(
                "", tk.END, iid=str(idx),
                values=(
                    v["placa"],
                    v["tipo_doc_runt"],
                    v["nro_doc"],
                    v["propietario"],
                    est,
                    emb_display,
                    f"{v['num_imgs']} img"
                ),
                tags=(tag,)
            )

    def _on_tree_select(self, event=None):
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        self.current_index = idx
        v = self.vehicles[idx]
        self.lbl_active_veh.configure(
            text=f"Seleccionado: {v['placa']} | {v['tipo_doc_runt']} {v['nro_doc']}\n{v['propietario']}"
        )

    def _open_selected_expediente(self):
        if not self._ensure_authenticated():
            return
        sel = self.tree.selection()
        idx = int(sel[0]) if sel else self.current_index
        if idx is None or idx >= len(self.vehicles):
            messagebox.showinfo("Abrir Carpeta", "Selecciona primero un vehículo en la tabla.")
            return
        exp_dir = self.vehicles[idx]["exp_dir"]
        os.makedirs(exp_dir, exist_ok=True)
        subprocess.Popen(["explorer", exp_dir])

    def _send_captcha_from_entry(self):
        if not self._ensure_authenticated():
            return
        txt = self.ent_captcha.get().strip()
        if not txt:
            return
        self.cmd_queue.put(("SUBMIT_CAPTCHA", txt))

    def _retry_with_doc_type(self, new_doc_type):
        if not self._ensure_authenticated():
            return
        sel = self.tree.selection()
        idx = int(sel[0]) if sel else self.current_index
        if idx is None or idx >= len(self.vehicles):
            return
        self.vehicles[idx]["tipo_doc_runt"] = new_doc_type
        self._populate_tree()
        self.tree.selection_set(str(idx))
        self.cmd_queue.put(("SINGLE", idx))

    def _run_selected_single(self):
        if not self._ensure_authenticated():
            return
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Consultar Placa", "Selecciona un vehículo de la tabla para consultarlo.")
            return
        idx = int(sel[0])
        self.is_paused = False
        self.cmd_queue.put(("SINGLE", idx))

    def _start_batch_process(self):
        if not self._ensure_authenticated():
            return
        if self.is_running_batch:
            self.is_paused = False
            self.log_message("Reanudando lote automático...")
            return
        self.is_running_batch = True
        self.is_paused = False
        self.cmd_queue.put(("START_BATCH", None))

    def _toggle_pause(self):
        if not self._ensure_authenticated():
            return
        self.is_paused = not self.is_paused
        if self.is_paused:
            self.log_message("⏸ Lote en pausa. Terminará el vehículo actual y se detendrá.")
            self.btn_pause.configure(text="▶ Continuar Lote", bg="#16a34a")
        else:
            self.log_message("▶ Continuando lote automático...")
            self.btn_pause.configure(text="⏸ Pausar Lote", bg="#d97706")
            self.cmd_queue.put(("START_BATCH", None))

    def _playwright_worker_loop(self):
        bot = RuntCiudadanoBot(log_callback=self.log_message)
        while True:
            try:
                cmd, payload = self.cmd_queue.get()
                if cmd == "CLOSE_BROWSER":
                    bot.stop_browser()
                    break
                if not self._ensure_authenticated():
                    continue

                if cmd == "SUBMIT_CAPTCHA":
                    bot.submit_captcha_from_gui(payload)
                elif cmd == "SINGLE":
                    self._process_one_vehicle(bot, payload)
                elif cmd == "START_BATCH":
                    for idx, v in enumerate(self.vehicles):
                        if self.is_paused or not self._ensure_authenticated():
                            break
                        if v["estado"] == "PENDIENTE":
                            self._process_one_vehicle(bot, idx)
                    self.is_running_batch = False
                    if not self.is_paused and self.session:
                        self.log_message("✅ Lote de vehículos pendientes finalizado.")
            except Exception as e:
                self.log_message(f"Error en trabajador Playwright: {e}")
                self.is_running_batch = False

    def _process_one_vehicle(self, bot, idx):
        if not self._ensure_authenticated():
            return

        v = self.vehicles[idx]
        placa = v["placa"]
        tipo_doc = v["tipo_doc_runt"]
        nro_doc = v["nro_doc"]
        exp_dir = v["exp_dir"]

        def _highlight_row():
            if hasattr(self, "tree") and self.tree.winfo_exists() and self.tree.exists(str(idx)):
                self.tree.selection_set(str(idx))
                self.tree.see(str(idx))
            if hasattr(self, "lbl_active_veh") and self.lbl_active_veh.winfo_exists():
                self.lbl_active_veh.configure(
                    text=f"Consultando ahora: {placa} | {tipo_doc}: {nro_doc}\n{v['propietario']}"
                )
        self.root.after(0, _highlight_row)

        bot.start_browser()

        while True:
            if not self._ensure_authenticated():
                return
            cap_bytes = bot.fill_vehicle_form(placa, tipo_doc, nro_doc)
            self._update_captcha_preview(cap_bytes)

            outcome, detail = self._wait_with_gui_captcha_support(bot)
            if outcome == "CAPTCHA_ERROR":
                self.log_message(f"Reintentando Captcha para {placa}...")
                continue
            break

        if outcome == "SUCCESS":
            num_imgs, analysis = bot.capture_full_vehicle_expediente(exp_dir, placa)
            tiene_emb = analysis.get("tiene_embargo", "NO")
            det_lim = analysis.get("detalle_limitacion", "NINGUNO")
            soat_est = analysis.get("soat_estado", "-")
            rtm_est = analysis.get("rtm_estado", "-")

            scanner_patios.save_consulta_result(
                v["patio"], v["anio"], placa, v["propietario"],
                tipo_doc, nro_doc, "COMPLETADO_CIUDADANO",
                tiene_emb, det_lim, soat_est, rtm_est, num_imgs, exp_dir
            )
            if tiene_emb != "NO":
                self.log_message(f"🚨 ALERTA {placa}: {det_lim}")
            else:
                self.log_message(f"✅ {placa} completado sin limitaciones (5 capturas archivadas).")

        elif outcome == "INCONSISTENCIA":
            num_imgs = bot.capture_inconsistencia(exp_dir, placa)
            scanner_patios.save_consulta_result(
                v["patio"], v["anio"], placa, v["propietario"],
                tipo_doc, nro_doc, "INCONSISTENCIA_PROPIETARIO",
                "NO", "No corresponde con propietarios activos", "-", "-", num_imgs, exp_dir
            )
            self.log_message(f"⚠ {placa}: Inconsistencia de propietario activo guardada.")

        self.root.after(0, self._scan_current_selection)

    def _wait_with_gui_captcha_support(self, bot, timeout_sec=240):
        import time
        start = time.time()
        page = bot.page
        while time.time() - start < timeout_sec:
            if not self._ensure_authenticated():
                return "SESSION_EXPIRED", "Sesión expirada"

            try:
                cmd, payload = self.cmd_queue.get_nowait()
                if cmd == "SUBMIT_CAPTCHA":
                    bot.submit_captcha_from_gui(payload)
            except queue.Empty:
                pass

            if not page or page.is_closed():
                return "CLOSED", ""

            try:
                btn_otra = page.locator("button:has-text('Realizar otra consulta')")
                if btn_otra.count() > 0 and btn_otra.first.is_visible():
                    page.wait_for_timeout(900)
                    return "SUCCESS", "OK"
            except Exception:
                pass

            try:
                modal_txt = page.evaluate("""() => {
                    const btns = Array.from(document.querySelectorAll('button'));
                    const aceptar = btns.find(b => b.innerText.trim() === 'Aceptar' && b.offsetParent !== null);
                    if (aceptar) {
                        const container = aceptar.closest('mat-dialog-container, .swal2-popup, .modal-content') || aceptar.parentElement.parentElement;
                        return (container?.innerText || '').trim();
                    }
                    return null;
                }""")
                if modal_txt:
                    up = modal_txt.upper()
                    if "NO CORRESPONDEN CON LOS PROPIETARIOS ACTIVOS" in up:
                        return "INCONSISTENCIA", modal_txt
                    elif "CAPTCHA" in up or "CARACTERES" in up or "IMAGEN" in up or "CODIGO" in up or "CÓDIGO" in up:
                        btn_ac = page.locator("button:has-text('Aceptar')")
                        if btn_ac.count() > 0:
                            btn_ac.first.click()
                        page.wait_for_timeout(600)
                        return "CAPTCHA_ERROR", modal_txt
                    else:
                        return "INCONSISTENCIA", modal_txt
            except Exception:
                pass

            page.wait_for_timeout(350)

        return "TIMEOUT", "Timeout"

    def _on_close_window(self):
        """
        Al cerrar la ventana, revoca inmediatamente el token en SQLite para que
        la próxima vez que se abra el programa obligatoriamente pida iniciar sesión.
        """
        self.is_paused = True
        self.is_running_batch = False
        if self.session and self.session.get("token"):
            auth_manager.logout_session(self.session["token"], "CIERRE_VENTANA_APLICACION")
        auth_manager.invalidate_all_active_sessions("CIERRE_TOTAL_APLICACION")
        try:
            self.cmd_queue.put_nowait(("CLOSE_BROWSER", None))
        except Exception:
            pass
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = AutomatizadorRuntGUI(root)
    root.mainloop()

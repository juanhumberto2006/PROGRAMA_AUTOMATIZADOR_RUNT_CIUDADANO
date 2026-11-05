# -*- coding: utf-8 -*-
"""
Motor de Automatización RUNT Ciudadano (Playwright + OCR opcional + Captura Inteligente de 5 Secciones).
- Abre Chrome en el Portal Público RUNT (Consulta Ciudadana por Placa y Propietario).
- Diligencia automáticamente: Procedencia (NACIONAL), Consultar Por (Placa y Propietario),
  Nro. placa, Tipo de Documento y Nro. documento.
- Extrae la imagen del Captcha en memoria (base64) para mostrarla en el Panel de Control
  y/o permitir que el usuario presione Enter o clic en 'Consultar Información'.
- Detecta automáticamente si:
  A) Aparece el modal de inconsistencia ('Los datos registrados no corresponden con los propietarios activos...'),
     toma la captura 01_RUNT_Ciudadano_Inconsistencia_Propietario_[PLACA].png y pasa al siguiente.
  B) Carga la ficha técnica del vehículo:
     1. Toma 01_RUNT_Ciudadano_Info_General_[PLACA].png
     2. Expande todos los paneles acordeón (SOAT, RTM, Solicitudes, Limitaciones a la Propiedad,
        Garantías a Favor De, Normalización y saneamiento).
     3. Toma las capturas seccionadas 02 a 05 dentro de EXPEDIENTE VEHICULAR.
     4. Analiza el DOM para detectar si hay EMBARGO, HURTO/FISCALÍA o PRENDA/GARANTÍA y estado de SOAT/RTM.
"""

import os
import time
import base64
from playwright.sync_api import sync_playwright

RUNT_URL = "https://portalpublico.runt.gov.co/#/consulta-vehiculo/consulta/consulta-ciudadana"

class RuntCiudadanoBot:
    def __init__(self, log_callback=None):
        self.pw = None
        self.browser = None
        self.context = None
        self.page = None
        self.log_callback = log_callback or (lambda msg: print(msg))

    def log(self, msg):
        try:
            self.log_callback(msg)
        except Exception:
            pass

    def start_browser(self):
        if self.page and not self.page.is_closed():
            return
        self.log("Iniciando Google Chrome para Portal RUNT Ciudadano...")
        self.pw = sync_playwright().start()
        try:
            self.browser = self.pw.chromium.launch(
                channel="chrome",
                headless=False,
                args=["--start-maximized", "--disable-blink-features=AutomationControlled"]
            )
        except Exception:
            self.browser = self.pw.chromium.launch(
                headless=False,
                args=["--start-maximized", "--disable-blink-features=AutomationControlled"]
            )
        self.context = self.browser.new_context(viewport={"width": 1366, "height": 860})
        self.page = self.context.new_page()
        self.navigate_to_form()

    def stop_browser(self):
        try:
            if self.browser:
                self.browser.close()
        except Exception:
            pass
        try:
            if self.pw:
                self.pw.stop()
        except Exception:
            pass
        self.page = None
        self.browser = None
        self.pw = None

    def navigate_to_form(self):
        if not self.page or self.page.is_closed():
            self.start_browser()
            return
        self.log("Cargando formulario de Consulta Automotores...")
        self.page.goto(RUNT_URL, wait_until="domcontentloaded", timeout=45000)
        self.page.wait_for_selector("input", timeout=25000)
        self.page.wait_for_timeout(1200)

    def ensure_on_search_form(self):
        """
        Verifica si estamos en el formulario de búsqueda; si estamos en una ficha de resultados,
        hace clic en 'Realizar otra consulta' o recarga la URL del formulario.
        """
        if not self.page or self.page.is_closed():
            self.start_browser()
            return

        try:
            # Cerrar cualquier modal de SweetAlert / MatDialog abierto previamente
            btn_aceptar = self.page.locator("button:has-text('Aceptar')")
            if btn_aceptar.count() > 0 and btn_aceptar.first.is_visible():
                btn_aceptar.first.click()
                self.page.wait_for_timeout(500)
        except Exception:
            pass

        try:
            btn_otra = self.page.locator("button:has-text('Realizar otra consulta')")
            if btn_otra.count() > 0 and btn_otra.first.is_visible():
                btn_otra.first.click()
                self.page.wait_for_timeout(1200)
                return
        except Exception:
            pass

        # Comprobar si están visibles los inputs del formulario
        if self.page.locator("input").count() < 2:
            self.navigate_to_form()

    def fill_vehicle_form(self, placa, tipo_doc_runt, nro_doc):
        """
        Diligencia automáticamente Placa, Tipo de Documento y Número de Documento,
        y deja el foco en el campo del Captcha.
        Retorna los bytes PNG del Captcha actual (si está disponible) para mostrarlo también en la GUI.
        """
        self.ensure_on_search_form()
        page = self.page

        # Esperar a que los campos estén presentes
        page.wait_for_selector("input", timeout=20000)
        page.wait_for_timeout(500)

        # Seleccionar Tipo de Documento si es diferente al actual
        try:
            doc_select = page.locator("mat-select[formcontrolname='tipoDocumento']")
            if doc_select.count() > 0:
                current_doc_text = doc_select.first.inner_text().strip()
                if tipo_doc_runt.upper() not in current_doc_text.upper():
                    doc_select.first.click()
                    page.wait_for_timeout(400)
                    opt = page.locator(f"mat-option:has-text('{tipo_doc_runt}')")
                    if opt.count() > 0:
                        opt.first.click()
                        page.wait_for_timeout(400)
                    else:
                        page.keyboard.press("Escape")
        except Exception as e:
            self.log(f"Aviso seleccionando tipo doc: {e}")

        # Llenar Placa, Documento y enfocar Captcha usando formcontrolname exacto
        placa_input = page.locator("input[formcontrolname='placa']")
        doc_input = page.locator("input[formcontrolname='documento']")
        captcha_input = page.locator("input[formcontrolname='captcha']")

        if placa_input.count() > 0 and doc_input.count() > 0:
            placa_input.first.click()
            placa_input.first.fill("")
            placa_input.first.fill(str(placa).strip().upper())

            doc_input.first.click()
            doc_input.first.fill("")
            doc_input.first.fill(str(nro_doc).strip())

            if captcha_input.count() > 0:
                captcha_input.first.click()
                captcha_input.first.fill("")
            page.wait_for_timeout(350)

        self.log(f"Formulario listo -> Placa: {placa} | {tipo_doc_runt}: {nro_doc}. Esperando CAPTCHA...")
        return self.extract_captcha_bytes()

    def extract_captcha_bytes(self):
        """Extrae la imagen base64 del captcha desde el DOM."""
        try:
            data_url = self.page.evaluate("""() => {
                const imgs = Array.from(document.querySelectorAll('img'));
                const cap = imgs.find(i => i.src && i.src.startsWith('data:image'));
                return cap ? cap.src : null;
            }""")
            if data_url and "," in data_url:
                b64_part = data_url.split(",", 1)[1]
                return base64.b64decode(b64_part)
        except Exception:
            pass
        return None

    def submit_captcha_from_gui(self, captcha_text):
        """
        Permite enviar el texto del captcha directamente desde la caja de la GUI
        o haciendo clic en 'Consultar Información'.
        """
        if not self.page or self.page.is_closed():
            return
        cap_input = self.page.locator("input[formcontrolname='captcha']")
        if cap_input.count() > 0 and captcha_text:
            cap_input.first.click()
            cap_input.first.fill(captcha_text.strip())
            self.page.wait_for_timeout(200)
        btn = self.page.locator("button:has-text('Consultar Información')")
        if btn.count() > 0:
            btn.first.click()

    def wait_for_query_outcome(self, timeout_sec=180):
        """
        Espera a que el usuario digite el Captcha y presione 'Consultar Información' (o lo envíe desde la GUI),
        y detecta cuál fue el resultado del portal:
        - 'SUCCESS': Cargó la ficha del vehículo ('Realizar otra consulta' / 'NRO. DE LICENCIA DE TRÁNSITO')
        - 'INCONSISTENCIA': Apareció modal 'Los datos registrados no corresponden con los propietarios activos'
        - 'NO_EXISTE': Apareció modal indicando que el vehículo no existe
        - 'CAPTCHA_ERROR': Apareció modal indicando que el captcha no es válido (permite reintentar)
        - 'TIMEOUT': Se agotó el tiempo de espera
        """
        start = time.time()
        page = self.page
        while time.time() - start < timeout_sec:
            if not page or page.is_closed():
                return "CLOSED", ""

            # 1. Revisar si ya cargó la vista de resultados del vehículo
            try:
                if page.locator("button:has-text('Realizar otra consulta')").count() > 0:
                    if page.locator("button:has-text('Realizar otra consulta')").first.is_visible():
                        page.wait_for_timeout(1000)
                        return "SUCCESS", "Ficha vehicular cargada"
            except Exception:
                pass

            # 2. Revisar si salió un diálogo / modal emergente
            try:
                modal_Info = page.evaluate("""() => {
                    const dialogs = Array.from(document.querySelectorAll('mat-dialog-container, .swal2-popup, .modal-content'));
                    for (const d of dialogs) {
                        const txt = (d.innerText || '').trim();
                        if (txt.length > 5) return txt;
                    }
                    // También buscar por el botón Aceptar visible
                    const btns = Array.from(document.querySelectorAll('button'));
                    const aceptar = btns.find(b => b.innerText.trim() === 'Aceptar' && b.offsetParent !== null);
                    if (aceptar && aceptar.parentElement && aceptar.parentElement.parentElement) {
                        return aceptar.parentElement.parentElement.innerText.trim();
                    }
                    return null;
                }""")
                if modal_Info:
                    upper_m = modal_Info.upper()
                    if "NO CORRESPONDEN CON LOS PROPIETARIOS ACTIVOS" in upper_m:
                        return "INCONSISTENCIA", modal_Info
                    elif "CAPTCHA" in upper_m or "CARACTERES" in upper_m or "CÓDIGO" in upper_m or "CODIGO" in upper_m:
                        self.log("Captcha incorrecto reportado por el portal. Cerrando alerta para reintentar...")
                        btn_ac = page.locator("button:has-text('Aceptar')")
                        if btn_ac.count() > 0:
                            btn_ac.first.click()
                        page.wait_for_timeout(600)
                        return "CAPTCHA_ERROR", modal_Info
                    else:
                        return "INCONSISTENCIA", modal_Info
            except Exception:
                pass

            page.wait_for_timeout(400)

        return "TIMEOUT", "Tiempo de espera agotado"

    def capture_inconsistencia(self, exp_dir, placa):
        """
        Toma la captura del modal 'Los datos registrados no corresponden con los propietarios activos...'
        y luego cierra el modal para dejar listo el siguiente vehículo.
        """
        os.makedirs(exp_dir, exist_ok=True)
        out_path = os.path.join(exp_dir, f"01_RUNT_Ciudadano_Inconsistencia_Propietario_{placa}.png")
        self.page.wait_for_timeout(400)
        self.page.screenshot(path=out_path, full_page=False)
        self.log(f"[GUARDADO] Captura de inconsistencia: 01_RUNT_Ciudadano_Inconsistencia_Propietario_{placa}.png")

        try:
            btn_ac = self.page.locator("button:has-text('Aceptar')")
            if btn_ac.count() > 0 and btn_ac.first.is_visible():
                btn_ac.first.click()
                self.page.wait_for_timeout(500)
        except Exception:
            pass
        return 1

    def capture_full_vehicle_expediente(self, exp_dir, placa):
        """
        Ejecuta automáticamente todo el protocolo de 5 capturas de la ficha del vehículo:
        1. Captura 01: Información General del Vehículo (arriba).
        2. Expande todos los paneles acordeón cerrados (Datos Técnicos, Póliza SOAT, RTM, Solicitudes,
           Limitaciones a la Propiedad, Garantías a Favor De, Garantías Mobiliarias, Tarjeta de Operación,
           Normalización y saneamiento).
        3. Analiza el contenido de SOAT, RTM, Limitaciones a la Propiedad y Garantías a Favor De.
        4. Hace scroll progresivo y guarda las capturas 02, 03, 04 y 05 en EXPEDIENTE VEHICULAR.
        """
        os.makedirs(exp_dir, exist_ok=True)
        page = self.page

        # Asegurar scroll arriba del todo para la Captura 01
        page.evaluate("""() => {
            window.scrollTo(0, 0);
            const scrollers = document.querySelectorAll('mat-sidenav-content, .mat-drawer-content, main, html, body');
            scrollers.forEach(s => { s.scrollTop = 0; });
        }""")
        page.wait_for_timeout(600)

        cap1 = os.path.join(exp_dir, f"01_RUNT_Ciudadano_Info_General_{placa}.png")
        page.screenshot(path=cap1, full_page=False)
        self.log(f"[1/5] Guardada: 01_RUNT_Ciudadano_Info_General_{placa}.png")

        # Expandir todos los paneles acordeón (excepto el primero si ya está abierto)
        self.log("Expandiendo todos los acordeones del expediente (SOAT, RTM, Solicitudes, Limitaciones, Normalización)...")
        page.evaluate("""() => {
            const headers = Array.from(document.querySelectorAll('mat-expansion-panel-header'));
            headers.forEach(h => {
                const txt = (h.innerText || '').toUpperCase();
                // Información general del vehículo ya viene abierta; los demás se abren si están cerrados
                const isExpanded = h.getAttribute('aria-expanded') === 'true' || h.classList.contains('mat-expanded');
                if (!txt.includes('INFORMACIÓN GENERAL DEL VEHÍCULO') && !txt.includes('INFORMACION GENERAL DEL VEHICULO')) {
                    if (!isExpanded) {
                        h.click();
                    }
                }
            });
        }""")
        page.wait_for_timeout(1800)

        # Extraer diagnóstico jurídico y documental directamente del DOM expandido
        analysis = page.evaluate("""() => {
            const result = {
                soat_estado: 'SIN REGISTRO',
                rtm_estado: 'SIN REGISTRO',
                tiene_embargo: 'NO',
                detalle_limitacion: 'NINGUNO'
            };

            const panels = Array.from(document.querySelectorAll('mat-expansion-panel'));
            for (const p of panels) {
                const title = (p.querySelector('mat-expansion-panel-header')?.innerText || '').toUpperCase();
                const body = (p.innerText || '').trim();

                if (title.includes('PÓLIZA SOAT') || title.includes('POLIZA SOAT')) {
                    if (body.toUpperCase().includes('NO SE ENCONTRÓ INFORMACIÓN')) {
                        result.soat_estado = 'SIN REGISTRO';
                    } else if (body.toUpperCase().includes('VIGENTE') && !body.toUpperCase().includes('NO VIGENTE')) {
                        result.soat_estado = 'VIGENTE';
                    } else {
                        result.soat_estado = 'NO VIGENTE';
                    }
                }

                if (title.includes('TÉCNICO MECÁNICA') || title.includes('TECNICO MECANICA')) {
                    if (body.toUpperCase().includes('NO SE ENCONTRÓ INFORMACIÓN')) {
                        result.rtm_estado = 'SIN REGISTRO';
                    } else {
                        const rows = Array.from(p.querySelectorAll('tbody tr'));
                        if (rows.length > 0) {
                            const firstRow = rows[0].innerText.toUpperCase();
                            result.rtm_estado = firstRow.includes('\\tSI\\t') ? 'VIGENTE' : 'NO VIGENTE';
                        } else {
                            result.rtm_estado = 'NO VIGENTE';
                        }
                    }
                }

                if (title.includes('LIMITACIONES A LA PROPIEDAD')) {
                    if (!body.toUpperCase().includes('NO SE ENCONTRÓ INFORMACIÓN') && !body.toUpperCase().includes('NO SE ENCONTRO INFORMACION')) {
                        const rows = Array.from(p.querySelectorAll('tbody tr'));
                        if (rows.length > 0) {
                            result.tiene_embargo = 'SI';
                            result.detalle_limitacion = rows.map(r => r.innerText.replace(/\\s+/g, ' ').trim()).join(' | ');
                        }
                    }
                }

                if (title.includes('GARANTÍAS A FAVOR DE') || title.includes('GARANTIAS A FAVOR DE')) {
                    if (!body.toUpperCase().includes('NO SE ENCONTRÓ INFORMACIÓN') && !body.toUpperCase().includes('NO SE ENCONTRO INFORMACION')) {
                        const rows = Array.from(p.querySelectorAll('tbody tr'));
                        if (rows.length > 0 && result.tiene_embargo === 'NO') {
                            result.tiene_embargo = 'PRENDA/GARANTIA';
                            result.detalle_limitacion = 'PRENDA: ' + rows.map(r => r.innerText.replace(/\\s+/g, ' ').trim()).join(' | ');
                        }
                    }
                }
            }
            return result;
        }""")

        # Función auxiliar para hacer scroll hacia un panel por texto de su título
        def scroll_to_panel(keyword):
            page.evaluate("""(kw) => {
                const headers = Array.from(document.querySelectorAll('mat-expansion-panel-header'));
                const target = headers.find(h => (h.innerText || '').toUpperCase().includes(kw.toUpperCase()));
                if (target) {
                    target.scrollIntoView({behavior: 'instant', block: 'start'});
                    // Ajuste ligero por barra fija superior
                    const scrollers = document.querySelectorAll('mat-sidenav-content, .mat-drawer-content, main, html, body');
                    scrollers.forEach(s => { if (s.scrollTop > 70) s.scrollTop -= 70; });
                    window.scrollBy(0, -70);
                }
            }""", keyword)
            page.wait_for_timeout(550)

        # Captura 02: Datos Técnicos y Póliza SOAT
        scroll_to_panel("Datos Técnicos")
        cap2 = os.path.join(exp_dir, f"02_RUNT_Ciudadano_SOAT_{placa}.png")
        page.screenshot(path=cap2, full_page=False)
        self.log(f"[2/5] Guardada: 02_RUNT_Ciudadano_SOAT_{placa}.png")

        # Captura 03: RTM y Solicitudes
        scroll_to_panel("Certificado de revisión técnico")
        cap3 = os.path.join(exp_dir, f"03_RUNT_Ciudadano_RTM_Solicitudes_{placa}.png")
        page.screenshot(path=cap3, full_page=False)
        self.log(f"[3/5] Guardada: 03_RUNT_Ciudadano_RTM_Solicitudes_{placa}.png")

        # Captura 04: Limitaciones a la Propiedad y Garantías
        scroll_to_panel("Certificado de revisión de la DIJIN")
        suffix_lim = "Limitaciones_Propiedad"
        if analysis.get("tiene_embargo") == "SI":
            if "HURTO" in analysis.get("detalle_limitacion", "").upper():
                suffix_lim = "Limitaciones_Hurto_Fiscalia"
            else:
                suffix_lim = "Limitaciones_Embargo"
        elif analysis.get("tiene_embargo") == "PRENDA/GARANTIA":
            suffix_lim = "Limitaciones_Garantias"

        cap4 = os.path.join(exp_dir, f"04_RUNT_Ciudadano_{suffix_lim}_{placa}.png")
        page.screenshot(path=cap4, full_page=False)
        self.log(f"[4/5] Guardada: 04_RUNT_Ciudadano_{suffix_lim}_{placa}.png")

        # Captura 05: Normalización y saneamiento (final de la página)
        scroll_to_panel("Limitaciones a la Propiedad")
        cap5 = os.path.join(exp_dir, f"05_RUNT_Ciudadano_Normalizacion_{placa}.png")
        page.screenshot(path=cap5, full_page=False)
        self.log(f"[5/5] Guardada: 05_RUNT_Ciudadano_Normalizacion_{placa}.png")

        return 5, analysis

# -*- coding: utf-8 -*-
"""
Motor de Automatización RUNT Ciudadano Multi-Canal en Paralelo (Playwright Async + 4 a 6 Pestañas Simultáneas).
- Abre múltiples canales aislados (BrowserContexts independientes) en paralelo contra el Portal Público RUNT.
- Mientras el Canal #1 está consultando, expandiendo acordeones y tomando las 5 capturas en segundo plano,
  los Canales #2, #3 y #4 ya tienen precargadas las siguientes placas con sus respectivos CAPTCHAs listos.
- Permite resolver CAPTCHAs en ráfaga continua (Captcha -> Enter -> Captcha -> Enter) sin tiempos muertos.
- Detecta automáticamente:
  A) Modal de inconsistencia ('Los datos registrados no corresponden con los propietarios activos...'),
     toma la captura 01_RUNT_Ciudadano_Inconsistencia_Propietario_[PLACA].png y pasa al siguiente vehículo.
  B) Ficha técnica del vehículo:
     1. Toma 01_RUNT_Ciudadano_Info_General_[PLACA].png
     2. Expande todos los paneles acordeón (SOAT, RTM, Solicitudes, Limitaciones a la Propiedad,
        Garantías a Favor De, Normalización y saneamiento).
     3. Toma las capturas seccionadas 02 a 05 dentro de EXPEDIENTE VEHICULAR.
     4. Analiza el DOM para detectar si hay EMBARGO, HURTO/FISCALÍA o PRENDA/GARANTÍA y estado de SOAT/RTM.
"""

import os
import time
import base64
import asyncio
from playwright.async_api import async_playwright

RUNT_URL = "https://portalpublico.runt.gov.co/#/consulta-vehiculo/consulta/consulta-ciudadana"


class RuntChannelSlot:
    """
    Representa una pestaña (tab) independiente dentro de la MISMA ventana de Google Chrome.
    Todas las pestañas comparten un único BrowserContext para no abrir múltiples ventanas separadas.
    """
    def __init__(self, slot_id: int, shared_context, log_cb, ui_slot_cb, screenshot_lock=None):
        self.slot_id = slot_id
        self.context = shared_context
        self.log = log_cb
        self.ui_slot_cb = ui_slot_cb  # callback(slot_id, state_dict)
        self.screenshot_lock = screenshot_lock or asyncio.Lock()
        self.page = None
        self.captcha_queue = asyncio.Queue()
        self.current_vehicle = None
        self.busy = False

    def notify_ui(self, status_text: str, status_color: str = "#94a3b8",
                  placa: str = "-", doc_info: str = "-", propietario: str = "-",
                  captcha_bytes: bytes = None, ready_for_captcha: bool = False):
        try:
            self.ui_slot_cb(self.slot_id, {
                "status_text": status_text,
                "status_color": status_color,
                "placa": placa,
                "doc_info": doc_info,
                "propietario": propietario,
                "captcha_bytes": captcha_bytes,
                "ready_for_captcha": ready_for_captcha
            })
        except Exception:
            pass

    async def ensure_page(self):
        if self.page and not self.page.is_closed():
            return
        self.page = await self.context.new_page()
        await self.navigate_to_form()

    async def close(self):
        try:
            if self.page and not self.page.is_closed():
                await self.page.close()
        except Exception:
            pass
        self.page = None

    async def dismiss_any_modal(self):
        """
        Cierra de forma inmediata cualquier alerta SweetAlert2 (swal2) o MatDialog que esté
        bloqueando la pantalla (ej. 'Ha ocurrido un error') para no quedar colgado 30s.
        Retorna el texto del modal si había uno, o None.
        """
        if not self.page or self.page.is_closed():
            return None
        try:
            modal_txt = await self.page.evaluate("""() => {
                const swal = document.querySelector('.swal2-container');
                const dlg = document.querySelector('mat-dialog-container, .modal-content');
                let txt = '';
                if (swal) txt = (swal.innerText || '').trim();
                else if (dlg) txt = (dlg.innerText || '').trim();

                const btn = document.querySelector('.swal2-confirm, .swal2-cancel, mat-dialog-container button');
                if (btn && btn.offsetParent !== null) {
                    btn.click();
                    return txt || 'MODAL_CERRADO';
                }
                const btns = Array.from(document.querySelectorAll('button'));
                const okBtn = btns.find(b => {
                    const t = (b.innerText || '').trim().toUpperCase();
                    return (t === 'ACEPTAR' || t === 'OK' || t === 'CERRAR') && b.offsetParent !== null;
                });
                if (okBtn) {
                    okBtn.click();
                    return txt || 'MODAL_CERRADO';
                }
                return null;
            }""")
            if modal_txt:
                await self.page.wait_for_timeout(250)
            return modal_txt
        except Exception:
            return None

    async def navigate_to_form(self):
        if not self.page or self.page.is_closed():
            await self.ensure_page()
            return
        try:
            # En Angular (#/ruta), goto() sobre la misma URL con hash NO recarga la página; usar reload() si ya estamos ahí
            if "consulta-ciudadana" in (self.page.url or ""):
                await self.page.reload(wait_until="domcontentloaded", timeout=45000)
            else:
                await self.page.goto(RUNT_URL, wait_until="domcontentloaded", timeout=45000)
            await self.page.wait_for_selector("input[formcontrolname='placa']", timeout=25000)
            await self.dismiss_any_modal()
            await self.page.wait_for_timeout(450)
        except Exception:
            try:
                await self.page.goto("about:blank")
                await self.page.goto(RUNT_URL, wait_until="domcontentloaded", timeout=45000)
                await self.page.wait_for_selector("input[formcontrolname='placa']", timeout=25000)
                await self.dismiss_any_modal()
            except Exception:
                pass

    async def ensure_on_search_form(self):
        if not self.page or self.page.is_closed():
            await self.ensure_page()
            return

        modal_was = await self.dismiss_any_modal()
        if modal_was and "ERROR" in modal_was.upper():
            await self.navigate_to_form()
            return

        try:
            btn_otra = self.page.locator("button:has-text('Realizar otra consulta')")
            if await btn_otra.count() > 0 and await btn_otra.first.is_visible():
                await btn_otra.first.click(timeout=5000)
                await self.page.wait_for_selector("input[formcontrolname='placa']", timeout=12000)
                await self.dismiss_any_modal()
                await self.page.wait_for_timeout(250)
                return
        except Exception:
            pass

        if await self.page.locator("input[formcontrolname='placa']").count() < 1:
            await self.navigate_to_form()

    async def fill_vehicle_form(self, placa: str, tipo_doc_runt: str, nro_doc: str, propietario: str = ""):
        """
        Diligencia Placa, Tipo de Documento y Documento en este canal y extrae el Captcha en base64.
        Maneja inmediatamente cualquier modal de error del RUNT sin bloquear 30 segundos.
        """
        await self.ensure_on_search_form()
        page = self.page

        await page.wait_for_selector("input[formcontrolname='placa']", timeout=20000)
        await self.dismiss_any_modal()

        # Seleccionar Tipo de Documento solo si cambió respecto al valor actual
        try:
            doc_select = page.locator("mat-select[formcontrolname='tipoDocumento']")
            if await doc_select.count() > 0:
                current_doc_text = (await doc_select.first.inner_text()).strip()
                if tipo_doc_runt.upper() not in current_doc_text.upper():
                    await doc_select.first.click(timeout=5000)
                    await page.wait_for_timeout(200)
                    opt = page.locator(f"mat-option:has-text('{tipo_doc_runt}')")
                    if await opt.count() > 0:
                        await opt.first.click(timeout=5000)
                        await page.wait_for_timeout(200)
                    else:
                        await page.keyboard.press("Escape")
        except Exception:
            await self.dismiss_any_modal()

        placa_input = page.locator("input[formcontrolname='placa']")
        doc_input = page.locator("input[formcontrolname='documento']")
        captcha_input = page.locator("input[formcontrolname='captcha']")

        if await placa_input.count() > 0 and await doc_input.count() > 0:
            # fill() directo con timeout corto evita bloqueos largos si aparece un overlay
            try:
                await placa_input.first.fill(str(placa).strip().upper(), timeout=5000)
                await doc_input.first.fill(str(nro_doc).strip(), timeout=5000)
                if await captcha_input.count() > 0:
                    await captcha_input.first.fill("", timeout=5000)
            except Exception:
                # Si un modal bloqueó el llenado, cerrarlo, recargar y reintentar una vez
                await self.dismiss_any_modal()
                await self.navigate_to_form()
                await page.locator("input[formcontrolname='placa']").first.fill(str(placa).strip().upper(), timeout=8000)
                await page.locator("input[formcontrolname='documento']").first.fill(str(nro_doc).strip(), timeout=8000)
                if await page.locator("input[formcontrolname='captcha']").count() > 0:
                    await page.locator("input[formcontrolname='captcha']").first.fill("", timeout=5000)

        # Esperar hasta 8s a que el servidor del RUNT entregue la imagen del captcha (sin cortar peticiones en curso)
        cap_bytes = None
        for attempt in range(3):
            for _ in range(40):
                modal_err = await self.dismiss_any_modal()
                if modal_err:
                    self.log(f"[Pestaña #{self.slot_id + 1}] ⚠ Alerta RUNT al cargar Captcha: {modal_err[:50]}. Recargando...")
                    break

                cap_bytes = await self.extract_captcha_bytes()
                if cap_bytes:
                    return cap_bytes
                await page.wait_for_timeout(200)

            # Si no apareció la imagen del captcha tras 8s (o salió alerta), recargar la pestaña limpiamente
            if attempt < 2:
                self.log(f"[Pestaña #{self.slot_id + 1}] 🔄 Captcha no cargó para {placa}, recargando automáticamente...")
                await page.wait_for_timeout(400 + (self.slot_id * 250))
                await self.navigate_to_form()
                await self.dismiss_any_modal()
                try:
                    doc_select = page.locator("mat-select[formcontrolname='tipoDocumento']")
                    if await doc_select.count() > 0:
                        current_doc_text = (await doc_select.first.inner_text()).strip()
                        if tipo_doc_runt.upper() not in current_doc_text.upper():
                            await doc_select.first.click(timeout=5000)
                            await page.wait_for_timeout(200)
                            opt = page.locator(f"mat-option:has-text('{tipo_doc_runt}')")
                            if await opt.count() > 0:
                                await opt.first.click(timeout=5000)
                                await page.wait_for_timeout(200)
                            else:
                                await page.keyboard.press("Escape")
                except Exception:
                    await self.dismiss_any_modal()
                await page.locator("input[formcontrolname='placa']").first.fill(str(placa).strip().upper(), timeout=8000)
                await page.locator("input[formcontrolname='documento']").first.fill(str(nro_doc).strip(), timeout=8000)
                if await page.locator("input[formcontrolname='captcha']").count() > 0:
                    await page.locator("input[formcontrolname='captcha']").first.fill("", timeout=5000)

        return cap_bytes

    async def extract_captcha_bytes(self):
        """
        Extrae la imagen del captcha del DOM en <1ms (sin robar foco de pestaña):
        1. Busca el <img> dentro del mismo <form> o <mat-card> que contiene input[formcontrolname='captcha'].
        2. Fallback: captura directa del elemento img.img-fluid dentro del formulario.
        """
        try:
            data_url = await self.page.evaluate("""() => {
                const capInput = document.querySelector("input[formcontrolname='captcha']");
                const scope = capInput ? (capInput.closest('form, mat-card') || document) : document;
                const capImg = scope.querySelector('img.img-fluid, img.img-responsive')
                    || Array.from(scope.querySelectorAll('img')).find(i => i.src && i.src.startsWith('data:image') && i.src.length > 250);
                if (capImg && capImg.src && capImg.src.startsWith('data:image') && capImg.src.length > 250) {
                    return capImg.src;
                }
                return null;
            }""")
            if data_url and "," in data_url:
                b64_part = data_url.split(",", 1)[1]
                raw = base64.b64decode(b64_part)
                if len(raw) > 200:
                    return raw
        except Exception:
            pass

        # Fallback: captura directa del elemento de imagen del captcha dentro del formulario
        try:
            cap_loc = self.page.locator("form img.img-fluid, form img.img-responsive, mat-card img.img-fluid")
            if await cap_loc.count() > 0 and await cap_loc.first.is_visible():
                box = await cap_loc.first.bounding_box()
                if box and box["width"] >= 40 and box["height"] >= 15:
                    return await cap_loc.first.screenshot()
        except Exception:
            pass
        return None

    async def submit_captcha_text(self, captcha_text: str):
        if not self.page or self.page.is_closed():
            return
        await self.dismiss_any_modal()
        cap_input = self.page.locator("input[formcontrolname='captcha']")
        if await cap_input.count() > 0 and captcha_text:
            await cap_input.first.fill(captcha_text.strip(), timeout=5000)
            await self.page.wait_for_timeout(80)
        btn = self.page.locator("button:has-text('Consultar Información')")
        if await btn.count() > 0:
            await btn.first.click(timeout=5000)

    async def wait_for_captcha_and_outcome(self, check_auth_fn, is_stopped_fn, timeout_sec=300):
        """
        Espera ya sea a que el usuario envíe el Captcha desde la tarjeta de este Canal en la GUI
        (o directamente en la pestaña de Chrome) y detecta el resultado del RUNT.
        """
        start = time.time()
        page = self.page

        # Limpiar cola de captchas viejos de este slot
        while not self.captcha_queue.empty():
            try:
                self.captcha_queue.get_nowait()
            except Exception:
                break

        submitted_at = None

        while time.time() - start < timeout_sec:
            if is_stopped_fn() or not check_auth_fn():
                return "CANCELLED", "Detenido o sesión expirada"

            # ¿El usuario envió el Captcha desde la GUI para este canal?
            if not self.captcha_queue.empty():
                txt = await self.captcha_queue.get()
                if txt == "__CANCEL__":
                    return "CANCELLED", "Cancelado"
                if txt == "__REFRESH_CAPTCHA__":
                    await self.navigate_to_form()
                    return "CAPTCHA_ERROR", "Recarga manual de captcha"
                if self.current_vehicle:
                    v = self.current_vehicle
                    self.notify_ui(
                        status_text="⏳ Enviando consulta al RUNT...",
                        status_color="#38bdf8",
                        placa=v["placa"],
                        doc_info=f"{v['tipo_doc_runt']}: {v['nro_doc']}",
                        propietario=v["propietario"],
                        captcha_bytes=None,
                        ready_for_captcha=False
                    )
                await self.submit_captcha_text(txt)
                submitted_at = time.time()

            if not page or page.is_closed():
                return "CLOSED", ""

            # 1. ¿Cargó la ficha vehicular ('Realizar otra consulta')?
            try:
                btn_otra = page.locator("button:has-text('Realizar otra consulta')")
                if await btn_otra.count() > 0 and await btn_otra.first.is_visible():
                    await page.wait_for_timeout(550)
                    return "SUCCESS", "OK"
            except Exception:
                pass

            # 2. ¿Salió modal emergente (Inconsistencia de propietario, Captcha inválido o Error temporal del portal)?
            try:
                modal_txt = await page.evaluate("""() => {
                    const swal = document.querySelector('.swal2-container');
                    if (swal && swal.offsetParent !== null) {
                        return (swal.innerText || '').trim();
                    }
                    const btns = Array.from(document.querySelectorAll('button'));
                    const aceptar = btns.find(b => {
                        const t = (b.innerText || '').trim().toUpperCase();
                        return (t === 'ACEPTAR' || t === 'OK') && b.offsetParent !== null;
                    });
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
                    elif "HA OCURRIDO UN ERROR" in up or "ERROR" in up or "SERVICIO" in up or "INTENTE" in up:
                        self.log(f"[Canal #{self.slot_id + 1}] ⚠ Error temporal del portal RUNT ('Ha ocurrido un error'). Recargando canal...")
                        await self.dismiss_any_modal()
                        await self.navigate_to_form()
                        return "CAPTCHA_ERROR", modal_txt
                    elif "CAPTCHA" in up or "CARACTERES" in up or "IMAGEN" in up or "CODIGO" in up or "CÓDIGO" in up or "VALIDA" in up:
                        await self.dismiss_any_modal()
                        await page.wait_for_timeout(300)
                        return "CAPTCHA_ERROR", modal_txt
                    else:
                        return "INCONSISTENCIA", modal_txt
            except Exception:
                pass

            # Si ya pasaron más de 25 segundos después de enviar el captcha y no respondió el portal, reintentar
            if submitted_at and (time.time() - submitted_at > 28):
                return "CAPTCHA_ERROR", "Tiempo de respuesta agotado tras enviar captcha"

            await asyncio.sleep(0.22)

        return "TIMEOUT", "Timeout"

    async def capture_inconsistencia(self, exp_dir: str, placa: str):
        os.makedirs(exp_dir, exist_ok=True)
        out_path = os.path.join(exp_dir, f"01_RUNT_Ciudadano_Inconsistencia_Propietario_{placa}.png")
        async with self.screenshot_lock:
            await self.page.bring_to_front()
            await self.page.wait_for_timeout(150)
            await self.page.screenshot(path=out_path, full_page=False)
        self.log(f"[Pestaña #{self.slot_id + 1}] ⚠ Inconsistencia guardada: {placa}")

        await self.dismiss_any_modal()
        return 1

    async def capture_full_vehicle_expediente(self, exp_dir: str, placa: str):
        """
        Toma las 5 capturas reglamentarias de forma ultra rápida (~2.2 segundos en total),
        sincronizando el foco de la pestaña para que cada captura salga nítida sin colisiones.
        """
        os.makedirs(exp_dir, exist_ok=True)
        page = self.page

        # Expandir todos los paneles acordeón cerrados y analizar el DOM sin robar foco todavía
        await page.evaluate("""() => {
            const headers = Array.from(document.querySelectorAll('mat-expansion-panel-header'));
            headers.forEach(h => {
                const txt = (h.innerText || '').toUpperCase();
                const isExpanded = h.getAttribute('aria-expanded') === 'true' || h.classList.contains('mat-expanded');
                if (!txt.includes('INFORMACIÓN GENERAL DEL VEHÍCULO') && !txt.includes('INFORMACION GENERAL DEL VEHICULO')) {
                    if (!isExpanded) {
                        h.click();
                    }
                }
            });
        }""")
        await page.wait_for_timeout(750)

        # Extraer diagnóstico jurídico y documental del DOM expandido
        analysis = await page.evaluate("""() => {
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

        async def scroll_to_panel(keyword):
            await page.evaluate("""(kw) => {
                const headers = Array.from(document.querySelectorAll('mat-expansion-panel-header'));
                const target = headers.find(h => (h.innerText || '').toUpperCase().includes(kw.toUpperCase()));
                if (target) {
                    target.scrollIntoView({behavior: 'instant', block: 'start'});
                    const scrollers = document.querySelectorAll('mat-sidenav-content, .mat-drawer-content, main, html, body');
                    scrollers.forEach(s => { if (s.scrollTop > 70) s.scrollTop -= 70; });
                    window.scrollBy(0, -70);
                }
            }""", keyword)
            await page.wait_for_timeout(160)

        # Turno exclusivo de captura visual sobre la ventana única para que las 5 fotos salgan nítidas
        async with self.screenshot_lock:
            await page.bring_to_front()
            await page.evaluate("""() => {
                window.scrollTo(0, 0);
                const scrollers = document.querySelectorAll('mat-sidenav-content, .mat-drawer-content, main, html, body');
                scrollers.forEach(s => { s.scrollTop = 0; });
            }""")
            await page.wait_for_timeout(150)

            cap1 = os.path.join(exp_dir, f"01_RUNT_Ciudadano_Info_General_{placa}.png")
            await page.screenshot(path=cap1, full_page=False)

            await scroll_to_panel("Datos Técnicos")
            cap2 = os.path.join(exp_dir, f"02_RUNT_Ciudadano_SOAT_{placa}.png")
            await page.screenshot(path=cap2, full_page=False)

            await scroll_to_panel("Certificado de revisión técnico")
            cap3 = os.path.join(exp_dir, f"03_RUNT_Ciudadano_RTM_Solicitudes_{placa}.png")
            await page.screenshot(path=cap3, full_page=False)

            await scroll_to_panel("Certificado de revisión de la DIJIN")
            suffix_lim = "Limitaciones_Propiedad"
            if analysis.get("tiene_embargo") == "SI":
                if "HURTO" in analysis.get("detalle_limitacion", "").upper():
                    suffix_lim = "Limitaciones_Hurto_Fiscalia"
                else:
                    suffix_lim = "Limitaciones_Embargo"
            elif analysis.get("tiene_embargo") == "PRENDA/GARANTIA":
                suffix_lim = "Limitaciones_Garantias"

            cap4 = os.path.join(exp_dir, f"04_RUNT_Ciudadano_{suffix_lim}_{placa}.png")
            await page.screenshot(path=cap4, full_page=False)

            await scroll_to_panel("Limitaciones a la Propiedad")
            cap5 = os.path.join(exp_dir, f"05_RUNT_Ciudadano_Normalizacion_{placa}.png")
            await page.screenshot(path=cap5, full_page=False)

        return 5, analysis


class RuntParallelCluster:
    """
    Orquestador asíncrono que administra N canales simultáneos (por defecto 4 pestañas aisladas).
    Permite procesar lotes masivos 4x a 5x más rápido pre-cargando formularios y captchas en paralelo.
    """
    def __init__(self, log_cb, ui_slot_cb, on_vehicle_done_cb, check_auth_fn):
        self.log = log_cb
        self.ui_slot_cb = ui_slot_cb
        self.on_vehicle_done_cb = on_vehicle_done_cb
        self.check_auth_fn = check_auth_fn

        self.pw = None
        self.browser = None
        self.shared_context = None
        self.screenshot_lock = asyncio.Lock()
        self.slots = []
        self.is_paused = False
        self.stop_requested = False

    async def start_cluster(self, num_channels: int = 4):
        if not self.browser:
            self.log(f"🚀 Iniciando 1 ventana de Chrome con {num_channels} pestañas simultáneas...")
            self.pw = await async_playwright().start()
            try:
                self.browser = await self.pw.chromium.launch(
                    channel="chrome",
                    headless=False,
                    args=["--start-maximized", "--disable-blink-features=AutomationControlled"]
                )
            except Exception:
                self.browser = await self.pw.chromium.launch(
                    headless=False,
                    args=["--start-maximized", "--disable-blink-features=AutomationControlled"]
                )

        if not self.shared_context:
            # Un único contexto compartido = 1 sola ventana de Chrome con múltiples pestañas
            self.shared_context = await self.browser.new_context(viewport={"width": 1366, "height": 860})

        # Ajustar cantidad de pestañas activas si creció
        while len(self.slots) < num_channels:
            slot_id = len(self.slots)
            slot = RuntChannelSlot(slot_id, self.shared_context, self.log, self.ui_slot_cb, self.screenshot_lock)
            self.slots.append(slot)

        # Inicializar las pestañas escalonadas (0.45s entre cada una) dentro de la misma ventana
        async def _staggered_init(s, delay):
            if delay > 0:
                await asyncio.sleep(delay)
            s.notify_ui("🌐 Abriendo pestaña RUNT...", "#38bdf8")
            await s.ensure_page()

        init_tasks = [_staggered_init(self.slots[i], i * 0.45) for i in range(num_channels)]
        await asyncio.gather(*init_tasks, return_exceptions=True)

    async def stop_cluster(self):
        self.stop_requested = True
        for s in self.slots:
            await s.close()
        self.slots = []
        try:
            if self.shared_context:
                await self.shared_context.close()
        except Exception:
            pass
        self.shared_context = None
        try:
            if self.browser:
                await self.browser.close()
        except Exception:
            pass
        try:
            if self.pw:
                await self.pw.stop()
        except Exception:
            pass
        self.browser = None
        self.pw = None

    def route_captcha_to_slot(self, slot_id: int, captcha_text: str):
        if 0 <= slot_id < len(self.slots):
            self.slots[slot_id].captcha_queue.put_nowait(captcha_text)

    async def process_vehicle_on_slot(self, slot: RuntChannelSlot, idx: int, v: dict):
        slot.busy = True
        slot.current_vehicle = v
        placa = v["placa"]
        tipo_doc = v["tipo_doc_runt"]
        nro_doc = v["nro_doc"]
        propietario = v["propietario"]
        exp_dir = v["exp_dir"]

        try:
            while not self.is_paused and not self.stop_requested and self.check_auth_fn():
                slot.notify_ui(
                    status_text=f"⚡ Precargando datos de {placa}...",
                    status_color="#facc15",
                    placa=placa,
                    doc_info=f"{tipo_doc}: {nro_doc}",
                    propietario=propietario,
                    captcha_bytes=None,
                    ready_for_captcha=False
                )

                cap_bytes = await slot.fill_vehicle_form(placa, tipo_doc, nro_doc, propietario)

                if not cap_bytes:
                    self.log(f"[Pestaña #{slot.slot_id + 1}] 🔄 Sin imagen de Captcha para {placa}, recargando pestaña...")
                    await slot.navigate_to_form()
                    continue

                slot.notify_ui(
                    status_text="🟢 LISTO: Digita Captcha + Enter",
                    status_color="#4ade80",
                    placa=placa,
                    doc_info=f"{tipo_doc}: {nro_doc}",
                    propietario=propietario,
                    captcha_bytes=cap_bytes,
                    ready_for_captcha=True
                )

                outcome, detail = await slot.wait_for_captcha_and_outcome(
                    self.check_auth_fn,
                    lambda: self.is_paused or self.stop_requested
                )

                if outcome == "CAPTCHA_ERROR":
                    self.log(f"[Canal #{slot.slot_id + 1}] 🔄 Recargando Captcha para {placa}...")
                    continue
                break

            if outcome == "SUCCESS":
                slot.notify_ui(
                    status_text=f"📸 Capturando 5 evidencias de {placa}...",
                    status_color="#38bdf8",
                    placa=placa,
                    doc_info=f"{tipo_doc}: {nro_doc}",
                    propietario=propietario,
                    captcha_bytes=None,
                    ready_for_captcha=False
                )
                num_imgs, analysis = await slot.capture_full_vehicle_expediente(exp_dir, placa)
                self.on_vehicle_done_cb(idx, v, "COMPLETADO_CIUDADANO", num_imgs, analysis)
                slot.notify_ui(
                    status_text=f"✅ {placa} guardado (5 capturas)",
                    status_color="#4ade80",
                    placa=placa,
                    doc_info=f"{tipo_doc}: {nro_doc}",
                    propietario=propietario,
                    captcha_bytes=None,
                    ready_for_captcha=False
                )
            elif outcome == "INCONSISTENCIA":
                slot.notify_ui(
                    status_text=f"📸 Guardando inconsistencia de {placa}...",
                    status_color="#fb923c",
                    placa=placa,
                    doc_info=f"{tipo_doc}: {nro_doc}",
                    propietario=propietario,
                    captcha_bytes=None,
                    ready_for_captcha=False
                )
                num_imgs = await slot.capture_inconsistencia(exp_dir, placa)
                self.on_vehicle_done_cb(idx, v, "INCONSISTENCIA_PROPIETARIO", num_imgs, {
                    "tiene_embargo": "NO",
                    "detalle_limitacion": "No corresponde con propietarios activos",
                    "soat_estado": "-",
                    "rtm_estado": "-"
                })
                slot.notify_ui(
                    status_text=f"⚠ {placa}: Inconsistencia registrada",
                    status_color="#fb923c",
                    placa=placa,
                    doc_info=f"{tipo_doc}: {nro_doc}",
                    propietario=propietario,
                    captcha_bytes=None,
                    ready_for_captcha=False
                )
        except Exception as e:
            self.log(f"[Canal #{slot.slot_id + 1}] Error procesando {placa}: {e}")
            slot.notify_ui(
                status_text=f"❌ Error en {placa}: reintentando canal",
                status_color="#f87171",
                placa=placa,
                doc_info=f"{tipo_doc}: {nro_doc}",
                propietario=propietario,
                captcha_bytes=None,
                ready_for_captcha=False
            )
            try:
                await slot.navigate_to_form()
            except Exception:
                pass
        finally:
            slot.busy = False
            slot.current_vehicle = None

    async def run_batch_parallel(self, indexed_vehicles: list, num_channels: int = 4):
        """
        Distribuye la cola de vehículos pendientes entre los N canales simultáneos.
        Apenas un canal termina de guardar las 5 capturas de una placa, toma inmediatamente
        la siguiente placa de la cola sin esperar a los demás canales.
        """
        self.is_paused = False
        self.stop_requested = False
        await self.start_cluster(num_channels)

        queue_pending = asyncio.Queue()
        for idx, v in indexed_vehicles:
            queue_pending.put_nowait((idx, v))

        total_q = queue_pending.qsize()
        self.log(f"⚡ Modo Ráfaga activo: {total_q} vehículos en cola distribuidos en {num_channels} canales simultáneos.")

        async def _worker(slot: RuntChannelSlot):
            while not self.is_paused and not self.stop_requested and self.check_auth_fn():
                if queue_pending.empty():
                    slot.notify_ui("🏁 Canal libre (Sin más pendientes)", "#94a3b8")
                    break
                try:
                    idx, v = queue_pending.get_nowait()
                except asyncio.QueueEmpty:
                    break
                await self.process_vehicle_on_slot(slot, idx, v)
                queue_pending.task_done()

        workers = [asyncio.create_task(_worker(self.slots[i])) for i in range(num_channels)]
        await asyncio.gather(*workers, return_exceptions=True)

        if not self.is_paused and not self.stop_requested and self.check_auth_fn():
            self.log("🎉 Lote en Modo Ráfaga finalizado.")

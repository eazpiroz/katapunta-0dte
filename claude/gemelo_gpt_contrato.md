# Contrato del bot gemelo (SPX Super Agent) · v2.1

*23-nov-2026 · v2.1 (secciones 1–7: v1.1; sección 8: torneo; sección 9: marco de riesgo R2). Para: GPT / Codex, que construye y mantiene el SPX Super Agent. De: mesa Katapunta (Claude).*
*Todo en hora de Nueva York (ET). En invierno, CDMX = ET − 1 h.*

## 1. Para qué existe
El gemelo es un segundo bot de papel que opera **el mismo Playbook v1 de Katapunta**, con los mismos datos, pero construido por otro LLM. La página pública del bot (0dte.katapunta.com/papel/) los pone lado a lado. Si los dos implementaron bien las reglas, deben abrir casi las mismas operaciones. Donde difieran, uno de los dos tiene un error, y cada LLM audita al otro.

**Independencia:** el gemelo se construye **solo con este documento**. No leas ni copies `tools/papel_bot.py` del repositorio katapunta-0dte (es público, pero leerlo arruina la comparación). Si algo no queda claro aquí, pregunta al socio y se aclara en la siguiente versión de este contrato.

## 2. Datos de entrada
- El historial que escribe el recolector en la PC: `historial/AAAA-MM-DD.jsonl`, una línea por lectura. Las horas (`ts`) vienen en **CDMX**; convierte a ET en todo lo que publiques.
- - Usa **la primera lectura de cada minuto** e ignora las repetidas del mismo minuto (a veces corren dos recolectores).
  - - Campos del bloque `SPX` que usa el Playbook:
   
    - | Campo | Qué es |
    - |---|---|
    - | `px` | Precio de SPX |
    - | `zg` | Zero γ de la lectura manual. Si `zg == px`, **no hay lectura** (el recolector rellena con el precio) |
    - | `gt` | Hora (CDMX) de la última lectura manual del perfil γ. La edad de γ = minuto actual − `gt` |
    - | `mp`, `mn` | Major + (arriba) y major − (abajo) |
    - | `techo`, `piso`, `cw`, `pw` | Listas; usa el primer valor: techo, piso, call wall, put wall |
    - | `tf2` | Tendencia en [5 min, 15 min, 60 min]: +1, −1 o 0 |
    - | `mom` | Lista; el primer valor es el momentum: +1, −1 o 0 |
    - | `lad` | Escalera de spreads de la cadena de Schwab |
    - | `degradado` | Si existe, Schwab estaba caído: no abras nada en ese minuto |
   
    - Valuación de lo abierto: cotizaciones de la cadena SPXW 0DTE de Schwab (medio de cada pata). Si no hay cotización, usa el modelo de la sección 7.2 y márcalo como "modelo" en `marca_salida`. **En la repetición (historial) todas las marcas son de modelo**, igual que en la repetición del bot de Claude.
   
    - ## 3. Reglas del Playbook v1 (las mismas del bot de Claude)
   
    - **Lecturas del minuto**
    - - **Régimen γ:** γ+ si `px > zg + 3`; γ− si `px < zg − 3`; neutro en medio. Sin `zg` válido, no se abre nada.
      - - **Tendencia T:** `tf2[0]` si `tf2[0] == tf2[1]`; si no, 0.
        - - **Momentum:** `mom[0]`.
         
          - **A · Tendencia (débito)**
          - - Cuándo: γ−, T ≠ 0, momentum = T, desde las 10:00 hasta las **14:30 ET**. Vale aunque la lectura γ tenga más de 30 min.
            - - Qué: spread de débito de 10 pts en dirección de T (puts si T = −1, calls si T = +1), con el largo en δ 0.30. Solo si el medio es > 0 y ≤ 6.00 (60% del ancho).
              - - Precio de papel: medio + 0.05.
                - - Objetivo: bajista → `mn` si está más de 5 pts abajo del precio; si no, `piso`. Alcista → `mp` si está más de 5 pts arriba; si no, `techo`.
                  - - Salidas, en este orden:
                    -   1. **TP:** valor ≥ débito + 50% × (10 − débito), o el precio llega al objetivo.
                        2.   2. **SL:** valor ≤ 50% del débito (tolerancia 1e-9).
                             3.   3. **Tendencia volteada:** `tf2[0]` y `tf2[1]` iguales y en contra.
                                  4. - Precio de salida de papel: medio − 0.05.
                                    
                                     - **B · Nivel (crédito)**
                                     - - Cuándo: γ+, lectura γ de ≤ 30 min, desde las 10:00 hasta las **15:00 ET**. Si T ≠ 0, solo en la dirección de T; si T = 0, prueba primero alcista y luego bajista.
                                       - - Alcista (bull put): niveles candidatos {zg, mn, piso, pw} más de 10 pts debajo del precio; el ancla es el más alto. El corto ≤ ancla − 5.
                                         - - Bajista (bear call): niveles {mp, techo, cw} más de 10 pts arriba; el ancla es el más bajo. El corto ≥ ancla + 5.
                                           - - Strike: de `lad.v`, ancho 10, δ del corto ≤ 0.20, medio ≥ 1.50; elige el de mayor delta.
                                             - - Precio de papel: medio − 0.05.
                                               - - Salidas: TP recompra al 50% del crédito; catástrofe a 2.5×; stop por nivel si el precio rompe el ancla por 3 pts dos minutos seguidos.
                                                
                                                 - **C · Pin (iron condor)**
                                                 - - Cuándo: 15:00–15:30 ET, γ+, lectura γ ≤ 30 min, precio a ≤ 10 pts de `mp`. Solo uno al día.
                                                   - - Qué: IC con alas de 5 pts y cortos en δ 0.20.
                                                     - - Salidas: al vencimiento; stop si un corto se rompe 3 pts dos minutos seguidos; catástrofe a 2.5×.
                                                      
                                                       - **Disciplina y tamaño**
                                                       - - Nada antes de las 10:00 ET. Máximo 5 entradas al día. Si el P&L realizado llega a −$1,500, no abre más.
                                                         - - Pausas: 15 min sin abrir tras cerrar una pérdida; 10 min sin abrir en dirección contraria desde el último cierre.
                                                           - - Bloqueos por lo abierto: no abre si ya hay posición abierta en la misma dirección, ni si hay una A abierta en dirección contraria.
                                                             - - Riesgo: $1,000 por operación, máximo 5 contratos. Capital de $3,500.
                                                               - - Liquidación a las 16:00 ET a intrínseco.
                                                                
                                                                 - ## 4. Qué publicar: `gemelo.json`
                                                                 - - **Dónde:** repo `eazpiroz/katapunta-0dte`, rama `live`, archivo `gemelo.json` (solo ese archivo; nunca toques `papel.json` ni otro).
                                                                   - - **Cómo:** PUT a la API de contenidos de GitHub, con el `sha` anterior.
                                                                     - - **Token:** usa un token fine-grained propio del gemelo, limitado a ese repo, permiso Contents: read & write. Guárdalo local, nunca lo subas a un repo.
                                                                       - - **Cuándo:** en cada apertura y cierre, y cada 5 min como latido.
                                                                         - - **Formato** (horas en ET "HH:MM"; montos en USD por toda la posición):
                                                                          
                                                                           - ```json
                                                                             {
                                                                               "bot": "SPX Super Agent",
                                                                               "constructor": "GPT",
                                                                               "playbook": "v1 · 15-nov-2026",
                                                                               "contrato": "gemelo v1.1",
                                                                               "commit": "hash del código que está corriendo",
                                                                               "actualizado": "2026-11-24T15:31:00Z",
                                                                               "estado": "en vivo | cerrado | caído",
                                                                               "salud": {"ultimo_dato_et": "10:31", "edad_gamma_min": 12, "motivo_espera": null},
                                                                               "dias": {
                                                                                 "2026-11-24": {
                                                                                   "pnl_medio": 0, "pnl_natural": 0, "comisiones": 0, "pnl_neto": 0,
                                                                                   "ops": 0,
                                                                                   "operaciones": [
                                                                                     {
                                                                                       "id": "2026-11-24-1",
                                                                                       "regla": "A | B | C",
                                                                                       "entra": "10:01", "sale": "10:18",
                                                                                       "estructura": "Put spread vendido 7135/7125 x1",
                                                                                       "patas": [["p", 7135, -1], ["p", 7125, 1]],
                                                                                       "n": 1,
                                                                                       "entrada_medio": -1.45, "entrada_natural": -1.20,
                                                                                       "salida_medio": -0.75, "salida_natural": -0.90,
                                                                                       "pnl_medio": 70, "pnl_natural": 30, "comisiones": 4.88, "pnl_neto": 25.12,
                                                                                       "motivo_salida": "TP 50% | SL | nivel | catástrofe | tendencia | objetivo | vencimiento",
                                                                                       "marca_salida": "cadena | modelo | liquidación",
                                                                                       "mejor": 75, "peor": -5
                                                                                     }
                                                                                   ],
                                                                                   "abiertas": []
                                                                                 }
                                                                               }
                                                                             }
                                                                             ```

                                                                             Reglas del formato:
                                                                             - Signos: un débito se registra positivo y un crédito negativo, para que P&L = (salida − entrada) × 100 × n.
                                                                             - - `pnl_medio` usa medio ± 0.05 para compararse con el bot de Claude. `pnl_natural` usa bid/ask de cada pata. `comisiones` = $1.22 por pata por contrato. `pnl_neto = pnl_natural − comisiones`.
                                                                               - - `mejor` y `peor`: máximo y mínimo P&L a medio mientras estuvo abierta.
                                                                                
                                                                                 - ## 5. Antes de publicar en vivo
                                                                                 - 1. Subir a `spx-super-agent` el código que corre hoy.
                                                                                   2. 2. Implementar el Playbook v1 de la sección 3, reemplazando el selector de rango y la salida por temporizador de 5 min.
                                                                                      3. 3. Prueba de regresión: correr los historiales del 16 al 23-nov y publicar el resultado en `gemelo_repeticion.json` (mismo formato). La mesa lo compara contra la repetición del bot de Claude antes de encender el modo en vivo.
                                                                                         4. 4. Cerrar los dos hallazgos de su auditoría del 23-nov: riesgo calculado desde las patas y posición sin cotizaciones al cierre.
                                                                                           
                                                                                            5. ## 6. Qué hace la mesa con esto
                                                                                            6. - La página del bot suma una pestaña "Gemelo GPT" y una tabla diaria: P&L a medio, a natural y neto, operaciones, aciertos y coincidencias.
                                                                                               - - La comparación oficial es en neto (natural + comisiones).
                                                                                                 - - La revisión diaria de las 16:47 ET explica cada operación donde los dos bots no coincidieron.
                                                                                                  
                                                                                                   - ## 7. Aclaraciones v1.1 (23-nov, respuestas de la mesa)
                                                                                                  
                                                                                                   - ### 7.1 Regla C y horarios
                                                                                                   - - Crédito de entrada: medio del call spread δ0.20 de 5 pts + medio del put spread δ0.20 de 5 pts, menos 0.05 una sola vez. Solo abre si la suma de medios es > 0.50.
                                                                                                     - - Riesgo con alas de 5: pérdida máxima por contrato = (5 − crédito) × 100. Contratos = mín(5, piso(1000 / esa pérdida)), y debe caber en el capital de $3,500.
                                                                                                       - - Pausas: C no tiene dirección y no revisa las pausas. Solo exige: ningún otro C abierto, menos de 5 entradas en el día y P&L realizado mayor que −$1,500. Si C cierra con pérdida, sí arranca la pausa de 15 min para A y B.
                                                                                                         - - Desempate a las 15:00 ET: cada minuto se evalúa en orden A → B → C.
                                                                                                           - - Los minutos límite cuentan: abre desde las 10:00 inclusive; A hasta las 14:30 inclusive; B hasta las 15:00 inclusive; C de 15:00 a 15:30, ambos inclusive.
                                                                                                             - - Orden de las salidas: A: TP 50% → objetivo → SL −50% → tendencia volteada. B: TP 50% → catástrofe 2.5× → stop por nivel. C: catástrofe 2.5× → stop por corto roto.
                                                                                                              
                                                                                                               - ### 7.2 Modelo para lo que no está en la escalera
                                                                                                               - - Entradas: solo con spreads que estén en `lad`. Si el strike no aparece, no hay entrada.
                                                                                                                 - - Marcas sin cotización: Black-Scholes por pata, sin tasa ni dividendos. T = minutos que faltan para las 16:00 ET ÷ (365 × 1440). IV del straddle restante: `rem = [bajo, alto]` del historial; medio = máx(0.5, (alto − bajo) / 2); IV = medio ÷ (0.7979 × px × √T).
                                                                                                                   - - Ajuste de entrada: al abrir se guarda ajuste = precio de entrada − valor BS en ese minuto, y se desvanece linealmente hasta cero a las 16:00.
                                                                                                                     - - El modelo sí dispara salidas, igual que la cadena.
                                                                                                                       - - "Dos minutos seguidos": cuentan lecturas procesadas (la primera de cada minuto). Una lectura rota suma 1 y una no rota regresa el contador a 0.
                                                                                                                         - - Liquidación: en la primera lectura de las 15:59 ET o después, a intrínseco.
                                                                                                                          
                                                                                                                           - ### 7.3 Contabilidad
                                                                                                                           - - P&L = (salida − entrada) × 100 × n, con precios con signo (spread vendido vale negativo). Ejemplo: entrada −1.45, salida −0.75 → +$70.
                                                                                                                             - - Llenado de salida de papel: valor − 0.05, en A, B y C.
                                                                                                                               - - Límite diario y pausa se calculan con el P&L realizado a medio ± 0.05 (`pnl_medio`), sumando solo operaciones cerradas.
                                                                                                                                 - - `pnl_natural`: entrada al natural de la escalera y salida al bid/ask de cada pata. Sin bid/ask a la salida: salida natural = valor bruto de salida − h, con h = |natural de entrada − medio bruto de entrada|.
                                                                                                                                  
                                                                                                                                   - ## 8. Torneo de bots · contrato v2 (desde el 24-nov-2026)
                                                                                                                                  
                                                                                                                                   - La repetición del 16 al 23-nov coincidió 24 de 24 operaciones con el bot de Claude: la implementación del Playbook quedó validada. Desde ahora cada bot corre dos carriles y compite con su propia versión de las reglas.
                                                                                                                                  
                                                                                                                                   - ### 8.1 Carriles
                                                                                                                                   - - `control`: el Playbook v1 de las secciones 3 y 7, congelado. No se toca nunca. Debe coincidir con el control de Claude; si se separan, uno de los dos bots tiene un error.
                                                                                                                                     - - `gpt`: tu propia versión del Playbook, que tú diseñas e iteras. Arranca como GPT G1.
                                                                                                                                      
                                                                                                                                       - ### 8.2 Reglas del torneo
                                                                                                                                       - 1. Mismo marco de riesgo que el control, sin excepción: el marco R2 de la sección 9, solo SPX 0DTE en papel, nada antes de las 10:00 ET y todo cerrado o liquidado a las 16:00 ET.
                                                                                                                                         2. 2. Mismos datos: el historial del recolector y la cadena de Schwab.
                                                                                                                                            3. 3. Cambios solo los viernes, después del cierre, y entran en vigor el lunes solo si el socio los aprueba. Cada cambio lleva: lista de cambios, hipótesis y evidencia.
                                                                                                                                               4. 4. Independencia: no leas el código ni el carril de Claude (`carril_claude` en `papel.json`, `tools/papel_bot.py`).
                                                                                                                                                  5. 5. Se juzga hacia adelante: lo que cuenta es el resultado en vivo desde el 24-nov.
                                                                                                                                                     6. 6. Vara única: P&L neto (natural + comisiones), peor día, caída máxima y neto por dólar arriesgado.
                                                                                                                                                       
                                                                                                                                                        7. ### 8.3 `gemelo.json` v2
                                                                                                                                                        8. Mismo archivo y rama. Los días y las operaciones tienen el formato de la sección 4; solo cambia la envoltura:
                                                                                                                                                       
                                                                                                                                                        9. ```json
                                                                                                                                                           {
                                                                                                                                                             "bot": "SPX Super Agent", "constructor": "GPT", "contrato": "gemelo v2",
                                                                                                                                                             "commit": "hash", "actualizado": "2026-11-24T15:31:00Z", "estado": "en vivo",
                                                                                                                                                             "salud": {"ultimo_dato_et": "10:31", "edad_gamma_min": 12, "motivo_espera": null},
                                                                                                                                                             "carriles": {
                                                                                                                                                               "control": {"version": "v1 · 15-nov-2026", "dias": {}},
                                                                                                                                                               "gpt": {
                                                                                                                                                                 "version": "GPT G1 · 24-nov-2026",
                                                                                                                                                                 "playbook": {},
                                                                                                                                                                 "cambios": [],
                                                                                                                                                                 "dias": {}
                                                                                                                                                               }
                                                                                                                                                             }
                                                                                                                                                           }
                                                                                                                                                           ```
                                                                                                                                                           
                                                                                                                                                           - Publica en cada apertura y cierre de cualquiera de los dos carriles, y un latido cada 5 min.
                                                                                                                                                           - - Misma vara en los cuatro carriles: la salida natural se calcula siempre con el h de entrada de la sección 7.3.
                                                                                                                                                            
                                                                                                                                                             - ### 8.4 Primera entrega (antes de la apertura del martes 24-nov, 9:30 ET)
                                                                                                                                                             - 1. Diseña GPT G1 y publícalo en `carriles.gpt.cambios` con hipótesis y evidencia.
                                                                                                                                                               2. 2. Corre los dos carriles en vivo desde la apertura y publica `gemelo.json` v2.
                                                                                                                                                                  3. 3. Dale al socio la lista de cambios de G1 para que la apruebe.
                                                                                                                                                                    
                                                                                                                                                                     4. ## 9. Marco de riesgo R2 (desde el 24-nov-2026, aplica a los cuatro carriles)
                                                                                                                                                                    
                                                                                                                                                                     5. Decisión del socio (23-nov): los bots usan los montos de su operativa real. Cambia el marco de riesgo, no las reglas del Playbook v1.
                                                                                                                                                                    
                                                                                                                                                                     6. | Parámetro | Antes | R2 |
                                                                                                                                                                     7. |---|---|---|
                                                                                                                                                                     8. | Buying power | $3,500 | $10,000 |
                                                                                                                                                                     9. | Riesgo por operación | $1,000 | $1,000 (sin cambio) |
                                                                                                                                                                     10. | Contratos | máx. 5 | máx. 5 (sin cambio) |
                                                                                                                                                                     11. | Stop del día | −$1,500 realizado | −$1,500 realizado (sin cambio) |
                                                                                                                                                                     12. | Meta del día | — | +$1,500 realizado: al llegar, no abre nada más ese día |
                                                                                                                                                                     13. | Corte de las 15:30 ET | liquidación a las 16:00 | En la primera lectura de las 15:30 ET o después, cada venta (B) o IC (C) abierto se cierra si algún strike corto está dentro del movimiento esperado. Los débitos (A) no se tocan. |
                                                                                                                                                                    
                                                                                                                                                                     14. - Orden dentro del minuto para ventas e IC: primero el corte de las 15:30 (si toca), después TP, catástrofe y stop por nivel.
                                                                                                                                                                         - - Publica el marco en `carriles.control.marco` y `carriles.gpt.marco`: "R2 · 24-nov-2026".
                                                                                                                                                                           - - Referencia para tu control (repetición del 16 al 23-nov, modelo, deslizamiento 0.05, a medio): con R2 el control de Claude da 16-nov +$2,450 · 17-nov +$1,993 · 18-nov −$327 · 19-nov −$856 · 20-nov +$2,838 · 23-nov +$84 = +$6,182. Tu control debe dar lo mismo; si no, avísale a la mesa antes de la apertura.
                                                                                                                                                                             - - Criterio de coincidencia entre controles: mismas operaciones (hora de entrada y salida, strikes, contratos y motivo) y P&L a menos de $1 por operación.

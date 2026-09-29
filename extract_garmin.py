"""
Extrae datos de Garmin Connect (sueño, batería corporal, calorías, actividades)
y los escribe como JSON en el repo privado bluepulse-data.

Pensado para correr como GitHub Action con cron diario, pero funciona igual
en local para pruebas.

Variables de entorno necesarias:
  GARMIN_EMAIL, GARMIN_PASSWORD   -> credenciales de tu cuenta Garmin Connect
  DATA_REPO_TOKEN                 -> token de GitHub con permiso contents:write sobre bluepulse-data
  DATA_REPO                       -> "tu_usuario/bluepulse-data"
  DATA_BRANCH                     -> "main" (opcional, default "main")
  SYNC_DAYS_BACK                  -> cuántos días hacia atrás mirar si no hay last_sync.json (default 7)

Dependencias: garminconnect, requests  (ver requirements.txt)
"""

import base64
import json
import os
from datetime import date, datetime, timedelta

import requests
from garminconnect import Garmin

GITHUB_API = "https://api.github.com"


def env(name, default=None, required=False):
    val = os.environ.get(name, default)
    if required and not val:
        raise RuntimeError(f"Falta la variable de entorno {name}")
    return val


class DataRepo:
    """Wrapper mínimo sobre la API de Contents de GitHub para leer/escribir JSON."""

    def __init__(self, repo, branch, token):
        self.repo = repo
        self.branch = branch
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "bluepulse-garmin-sync",
        }

    def read_json(self, path):
        url = f"{GITHUB_API}/repos/{self.repo}/contents/{path}?ref={self.branch}"
        r = requests.get(url, headers=self.headers)
        if r.status_code == 404:
            return None, None
        r.raise_for_status()
        data = r.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        return json.loads(content), data["sha"]

    def write_json(self, path, value, message):
        _, sha = self.read_json(path)
        url = f"{GITHUB_API}/repos/{self.repo}/contents/{path}"
        body = {
            "message": message,
            "content": base64.b64encode(json.dumps(value, indent=2, ensure_ascii=False).encode("utf-8")).decode("ascii"),
            "branch": self.branch,
        }
        if sha:
            body["sha"] = sha
        r = requests.put(url, headers=self.headers, json=body)
        r.raise_for_status()
        return r.json()


def dias_a_sincronizar(repo: DataRepo):
    meta, _ = repo.read_json("garmin/last_sync.json")
    if meta and meta.get("ultima_fecha"):
        ultima = date.fromisoformat(meta["ultima_fecha"])
    else:
        dias_atras = int(env("SYNC_DAYS_BACK", "7"))
        ultima = date.today() - timedelta(days=dias_atras)

    hoy = date.today()
    fechas = []
    d = ultima
    while d <= hoy:
        fechas.append(d)
        d += timedelta(days=1)
    return fechas


def _sleep_dto(client: Garmin, fecha_str: str):
    """dailySleepDTO de Garmin para "fecha_str", o {} si no hay dato (reloj
    sin poner esa noche, fecha futura, error de red, etc.) — nunca lanza."""
    try:
        data = client.get_sleep_data(fecha_str)
        return (data or {}).get("dailySleepDTO") or {}
    except Exception:
        return {}


def extraer_wellness(client: Garmin, fecha: date):
    fecha_str = fecha.isoformat()
    snapshot = {"fecha": fecha_str}

    # Garmin guarda la noche D-1 -> D (la que termina la mañana de "fecha")
    # en el registro de sueño de "fecha", y la noche D -> D+1 (la que
    # empieza esa misma noche) en el registro de "fecha + 1 día". De ahí
    # sacamos "despertar" (hora en que se levantó esa mañana) y "acostarse"
    # (hora en que se fue a dormir esa noche), en epoch-ms UTC — la misma
    # unidad que usa bodyBatteryValuesArray, así que se pueden comparar
    # directamente sin líos de zona horaria.
    hoy_dto = _sleep_dto(client, fecha_str)
    manana_dto = _sleep_dto(client, (fecha + timedelta(days=1)).isoformat())
    despertar_ms = hoy_dto.get("sleepEndTimestampGMT")
    acostarse_ms = manana_dto.get("sleepStartTimestampGMT")

    seg = hoy_dto.get("sleepTimeSeconds")
    snapshot["sueno_horas"] = round(seg / 3600, 2) if seg else None

    try:
        body_battery = client.get_body_battery(fecha_str, fecha_str)
        if body_battery:
            # bodyBatteryValuesArray es una lista de [timestamp_ms, valor] a lo
            # largo del día natural (00:01-23:59). Si te acuestas después de
            # medianoche, ese día natural no es "tu día" — arranca a mitad de
            # la noche anterior, todavía dormido. Por eso se acota la ventana
            # a "desde que te despertaste esa mañana hasta que te acostaste
            # esa noche" usando los datos de sueño de arriba; si falta alguno
            # de los dos extremos (p.ej. hoy mismo, que aún no te has
            # acostado) no se recorta por ese lado. Si no hay NINGÚN dato de
            # sueño ese día (reloj sin poner), se cae al día natural completo
            # de toda la vida, para no dejar la tarjeta vacía.
            puntos_todos = [p for p in body_battery[0].get("bodyBatteryValuesArray", []) if p[1] is not None]
            puntos = [
                p for p in puntos_todos
                if (despertar_ms is None or p[0] >= despertar_ms)
                and (acostarse_ms is None or p[0] <= acostarse_ms)
            ]
            if not puntos:
                puntos = puntos_todos  # la ventana no solapó con datos reales -> fallback

            valores = [p[1] for p in puntos]
            snapshot["bateria_corporal"] = valores[-1] if valores else None
            snapshot["bateria_corporal_inicio"] = valores[0] if valores else None
            snapshot["bateria_corporal_min"] = min(valores) if valores else None
            snapshot["bateria_corporal_max"] = max(valores) if valores else None
            snapshot["bateria_corporal_serie"] = [
                [datetime.utcfromtimestamp(p[0] / 1000).strftime("%H:%M"), p[1]] for p in puntos
            ]
            snapshot["bateria_corporal_ventana"] = {
                "despertar": datetime.utcfromtimestamp(despertar_ms / 1000).strftime("%H:%M") if despertar_ms else None,
                "acostarse": datetime.utcfromtimestamp(acostarse_ms / 1000).strftime("%H:%M") if acostarse_ms else None,
            }
    except Exception as e:
        snapshot["bateria_corporal"] = None
        snapshot["_error_bateria"] = str(e)

    try:
        stats = client.get_stats(fecha_str)
        snapshot["kcal_totales"] = stats.get("totalKilocalories")
        snapshot["kcal_activas"] = stats.get("activeKilocalories")
        snapshot["kcal_pasivas"] = stats.get("bmrKilocalories")
        snapshot["pasos"] = stats.get("totalSteps")
        snapshot["fc_reposo"] = stats.get("restingHeartRate")
    except Exception as e:
        snapshot["_error_stats"] = str(e)

    # NOTA: antes se guardaba aquí el "training readiness" / "training
    # status" crudo de Garmin para el aviso de riesgo de lesión/descanso.
    # Se ha quitado a propósito: ese cálculo lo hace ahora la webapp por su
    # cuenta a partir de datos crudos (sueño, batería, FC reposo, carga de
    # entreno) para no depender de la fórmula de una marca concreta — y de
    # paso nos ahorramos dos llamadas más a la API de Garmin por día.

    return snapshot


def clasificar_tipo(activity_type: str) -> str:
    t = (activity_type or "").lower()
    if "run" in t:
        return "running"
    if "strength" in t or "fuerza" in t:
        return "fuerza"
    return "otro"


def extraer_actividades(client: Garmin, fecha: date):
    fecha_str = fecha.isoformat()
    actividades = client.get_activities_by_date(fecha_str, fecha_str)
    resultado = []
    for act in actividades:
        tipo = clasificar_tipo(act.get("activityType", {}).get("typeKey"))
        activity_id = act.get("activityId")
        entrada = {
            "id": activity_id,
            "fecha": fecha_str,
            "tipo": tipo,
            "tipo_garmin": act.get("activityType", {}).get("typeKey"),
            "duracion_min": round(act.get("duration", 0) / 60, 1) if act.get("duration") else None,
            "distancia_km": round(act.get("distance", 0) / 1000, 2) if act.get("distance") else None,
            "fc_media": act.get("averageHR"),
            "calorias": act.get("calories"),
            # Antes solo guardábamos 4 campos concretos (elevación, VO2max,
            # efecto aeróbico/anaeróbico) y se perdía todo lo demás que Garmin
            # ya incluye gratis en este mismo endpoint: cadencia, potencia,
            # zancada, tiempo de contacto con el suelo, tiempo de
            # recuperación recomendado, carga de entrenamiento, tiempo en
            # cada zona de FC, etc. Se guarda el dict completo tal cual —
            # nada que perder, y la webapp decide qué mostrar de cada tipo
            # de actividad.
            "raw_garmin": act,
        }

        # Para entrenos de fuerza, Garmin guarda las series (ejercicio, repeticiones,
        # peso) en un endpoint aparte. Se guarda el JSON crudo tal cual (la webapp
        # ya sabe interpretarlo) para no perder nada aunque el formato varíe.
        if tipo == "fuerza" and activity_id:
            try:
                entrada["ejercicios_raw"] = client.get_activity_exercise_sets(activity_id)
            except Exception as e:
                entrada["_error_ejercicios"] = str(e)

        resultado.append(entrada)
    return resultado


def main():
    email = env("GARMIN_EMAIL", required=True)
    password = env("GARMIN_PASSWORD", required=True)
    repo = DataRepo(
        repo=env("DATA_REPO", required=True),
        branch=env("DATA_BRANCH", "main"),
        token=env("DATA_REPO_TOKEN", required=True),
    )

    print("Conectando a Garmin Connect...")
    client = Garmin(email, password)
    client.login()

    fechas = dias_a_sincronizar(repo)
    print(f"Sincronizando {len(fechas)} día(s): {fechas[0]} -> {fechas[-1]}")

    for fecha in fechas:
        wellness = extraer_wellness(client, fecha)
        repo.write_json(
            f"garmin/wellness/{fecha.isoformat()}.json",
            wellness,
            f"Sync wellness {fecha.isoformat()}",
        )

        for act in extraer_actividades(client, fecha):
            repo.write_json(
                f"garmin/activities/{fecha.isoformat()}_{act['id']}.json",
                act,
                f"Sync actividad {act['id']} ({fecha.isoformat()})",
            )

        print(f"  {fecha}: bateria={wellness.get('bateria_corporal')} sueno={wellness.get('sueno_horas')}h")

    repo.write_json(
        "garmin/last_sync.json",
        {"ultima_fecha": fechas[-1].isoformat(), "sincronizado_en": datetime.utcnow().isoformat()},
        "Actualiza last_sync",
    )
    print("Listo.")


if __name__ == "__main__":
    main()

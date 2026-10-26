#!/usr/bin/env python3
"""
rt_veo.py - анімує кадри кліпу «Ключі у траві» через Veo 3.1 (RoyalTechno API).

ПАПКА (усе поруч зі скриптом):
  rt_veo.py
  scenes.json        - промпти руху S01-S43 (вже готовий)
  frames\\S01.png ... - кадри зі Storyflow (png / jpg / webp, 16:9)
  rt_key.txt         - твій API-ключ одним рядком. НІКОМУ не надсилай.

РЕЗУЛЬТАТ:
  clips\\720p\\S01.mp4 ...  (або clips\\1080p\\...)
  state.json        - журнал задач. Не видаляй, поки йде генерація:
                      саме він не дає заплатити за сцену двічі.

КОМАНДИ:
  python rt_veo.py check                          перевірити кадри і ключ (безкоштовно)
  python rt_veo.py price                          баланс і ціни Veo 3.1
  python rt_veo.py run --scenes S01,S02,S03       тест на 3 сценах, 720p
  python rt_veo.py run --all                      усі сцени, 720p
  python rt_veo.py run --all --res 1080p          фінал у 1080p
  python rt_veo.py status                         що готово, що ні

Перервав (Ctrl+C, вимкнувся ПК)? Запусти ту саму команду ще раз:
задачі, що вже в черзі, не відправляються повторно - скрипт їх дочекається.

Потрібно: Python 3.9+, pip install requests pillow
"""
import argparse
import base64
import io
import json
import os
import random
import sys
import time
import uuid
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("Немає бібліотеки requests. Виконай:  pip install requests pillow")

try:
    from PIL import Image  # потрібна лише для важких кадрів і перевірки 16:9
except ImportError:
    Image = None

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
API = os.environ.get("RT_API_BASE", "https://api.royaltechno.cc/v1")
MODEL = "veo-3.1"
MAX_IMAGE_BYTES = 5 * 1024 * 1024 - 64 * 1024  # ліміт API 5 MiB, з запасом
POLL_SEC = 10                                   # правило API для відео
DEFAULT_PARALLEL = 4
BACKOFF_BASE, BACKOFF_MAX, MAX_ATTEMPTS = 2, 60, 6
RETRYABLE = {429, 503}
STOP_ALL = {401, 402, 403}                      # ключ / баланс / бан - далі не відправляємо
TERMINAL = {"succeeded", "failed", "canceled"}
IN_FLIGHT = {"queued", "processing"}

SCENES_FILE = HERE / "scenes.json"
FRAMES_DIR = HERE / "frames"
STATE_FILE = HERE / "state.json"
KEY_FILE = HERE / "rt_key.txt"


# ---------- дрібниці ----------

def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def load_key():
    key = os.environ.get("RT_API_KEY", "").strip()
    if not key and KEY_FILE.exists():
        key = KEY_FILE.read_text(encoding="utf-8-sig").strip()
    if not key:
        sys.exit(f"Немає ключа. Створи файл {KEY_FILE.name} поруч зі скриптом і встав у нього ключ одним рядком.")
    return key


def load_scenes():
    data = json.loads(SCENES_FILE.read_text(encoding="utf-8"))
    return {s["scene"]: s for s in data["scenes"]}


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def backoff(attempt):
    return min(BACKOFF_MAX, BACKOFF_BASE * 2 ** attempt) * random.uniform(0.8, 1.2)


class Fatal(Exception):
    def __init__(self, status, error):
        self.status = status
        self.error = error or {}
        super().__init__(f"{status} {self.error.get('code')}: {self.error.get('message')}")


def error_of(r):
    try:
        return r.json().get("error") or {}
    except ValueError:
        return {"code": "http_" + str(r.status_code), "message": r.text[:200]}


# ---------- кадри ----------

def find_frame(scene):
    for ext in (".png", ".jpg", ".jpeg", ".webp", ".PNG", ".JPG", ".JPEG", ".WEBP"):
        p = FRAMES_DIR / f"{scene}{ext}"
        if p.exists():
            return p
    return None


def sniff_mime(raw):
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def image_size(raw, mime):
    if Image is not None:
        with Image.open(io.BytesIO(raw)) as im:
            return im.size
    if mime == "image/png":
        return int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big")
    return None


def to_jpeg(raw):
    """Стискає кадр у JPEG, поки не влізе в ліміт API."""
    if Image is None:
        raise RuntimeError("кадр важчий за 5 МБ - потрібна бібліотека pillow:  pip install pillow")
    with Image.open(io.BytesIO(raw)) as im:
        im = im.convert("RGB")
        for quality in (92, 88, 82, 75):
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=quality, optimize=True)
            if buf.tell() <= MAX_IMAGE_BYTES:
                return buf.getvalue()
        im.thumbnail((1920, 1080))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85, optimize=True)
        return buf.getvalue()


def prepare_frame(scene):
    """Повертає (data_uri, примітки) або кидає RuntimeError."""
    path = find_frame(scene)
    if path is None:
        raise RuntimeError(f"немає кадру frames\\{scene}.png (або .jpg/.webp)")
    raw = path.read_bytes()
    mime = sniff_mime(raw)
    if mime is None:
        raise RuntimeError(f"{path.name}: невідомий формат (потрібен PNG, JPEG або WebP)")
    notes = []
    size = image_size(raw, mime)
    if size:
        w, h = size
        if abs(w / h - 16 / 9) > 0.03:
            notes.append(f"пропорція {w}x{h} не 16:9 - кліп вийде такої ж форми")
    if len(raw) > MAX_IMAGE_BYTES:
        raw = to_jpeg(raw)
        mime = "image/jpeg"
        notes.append(f"стиснуто в JPEG ({len(raw) // 1024} КБ)")
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}", notes


# ---------- API ----------

class Client:
    def __init__(self, key):
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {key}"

    def call(self, method, path, **kw):
        last = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                r = self.s.request(method, f"{API}{path}", timeout=60, **kw)
            except requests.RequestException as e:
                last = e
                time.sleep(backoff(attempt))
                continue
            if r.status_code in RETRYABLE:
                err = error_of(r)
                if err.get("code") == "queue_full":
                    return r
                if r.status_code == 503 and err.get("param") == "input.resolution":
                    raise Fatal(r.status_code, err)
                wait = max(float(r.headers.get("Retry-After", 0) or 0), backoff(attempt))
                log(f"  сервер просить почекати ({r.status_code} {err.get('code')}), {wait:.0f} с")
                time.sleep(wait)
                continue
            if r.status_code >= 400 and r.status_code != 304:
                raise Fatal(r.status_code, error_of(r))
            return r
        raise RuntimeError(f"не вдалося після {MAX_ATTEMPTS} спроб: {method} {path} ({last})")

    def account(self):
        return self.call("GET", "/account").json()

    def pricing(self):
        return self.call("GET", "/pricing").json()

    def submit(self, payload, idem_key):
        r = self.call("POST", "/jobs", json=payload, headers={"Idempotency-Key": idem_key})
        if r.status_code == 429:
            return None, float(r.headers.get("Retry-After", 60) or 60)
        return r.json(), 0.0

    def poll(self, job_id, etag):
        r = self.call("GET", f"/jobs/{job_id}", headers={"If-None-Match": etag} if etag else {})
        if r.status_code == 304:
            return None, etag
        return r.json(), r.headers.get("ETag", etag)

    def download(self, url, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".part")
        with requests.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    f.write(chunk)
        tmp.replace(dest)


# ---------- команди ----------

def pick_scenes(args, scenes):
    if args.all:
        return list(scenes)
    if not args.scenes:
        sys.exit("Вкажи --scenes S01,S02 або --all")
    names = [x.strip().upper() for x in args.scenes.split(",") if x.strip()]
    bad = [n for n in names if n not in scenes]
    if bad:
        sys.exit(f"Немає таких сцен у scenes.json: {', '.join(bad)}")
    return names


def cmd_check(args):
    scenes = load_scenes()
    ok, problems = 0, 0
    for name in scenes:
        try:
            _, notes = prepare_frame(name)
            ok += 1
            if notes:
                log(f"{name}: {'; '.join(notes)}")
        except RuntimeError as e:
            problems += 1
            log(f"{name}: ПРОБЛЕМА - {e}")
    log(f"Кадрів готово: {ok} з {len(scenes)}" + (f", проблем: {problems}" if problems else ""))
    if Image is None:
        log("Порада: pip install pillow - тоді скрипт перевірить 16:9 для JPEG і стисне важкі кадри.")
    try:
        acc = Client(load_key()).account()
        log(f"Ключ працює. Баланс: {acc.get('balance_usd')}")
    except Fatal as e:
        log(f"Ключ НЕ працює: {e}")


def cmd_price(args):
    c = Client(load_key())
    acc = c.account()
    log(f"Баланс: {acc.get('balance_usd')}")
    plans = acc.get("plans") or []
    if plans:
        log(f"Активні плани: {len(plans)} (деталі в боті)")
    rows = [p for p in c.pricing().get("data", []) if p.get("model") == MODEL]
    for p in rows:
        log(f"{MODEL} · {p.get('resolution')} · {p.get('duration_sec')} с = {p.get('cost_usd')}")
    if not rows:
        log(f"{MODEL} зараз немає в прайсі (недоступна).")


def cmd_status(args):
    state = load_state()
    if not state:
        log("Ще нічого не запускали.")
        return
    total = 0
    for k in sorted(state):
        st = state[k]
        total += st.get("cost_cents", 0) if st.get("status") == "succeeded" else 0
        extra = st.get("file") or (st.get("error") or {}).get("message") or st.get("job_id") or ""
        log(f"{k:12} {st.get('status', '?'):11} {extra}")
    log(f"Витрачено на готові кліпи: ${total / 100:.2f}")


def cmd_run(args):
    scenes = load_scenes()
    names = pick_scenes(args, scenes)
    res = args.res
    out_dir = HERE / "clips" / res
    state = load_state()
    client = Client(load_key())

    try:
        acc = client.account()
        limit = (acc.get("limits") or {}).get("recommended_max_in_flight") or DEFAULT_PARALLEL
        log(f"Баланс: {acc.get('balance_usd')}")
    except Fatal as e:
        sys.exit(f"Ключ не працює: {e}")
    parallel = max(1, min(args.parallel, int(limit)))

    todo, inflight = [], {}
    for name in names:
        k = f"{name}@{res}"
        st = state.get(k)
        if st is None:
            todo.append(name)
        elif st["status"] in IN_FLIGHT:
            inflight[k] = {"etag": None, "next": 0.0}
            log(f"{name}: уже в черзі ({st['job_id']}) - чекаю, повторно не відправляю")
        elif st["status"] == "submitting":
            todo.append(name)  # той самий Idempotency-Key - сервер не створить дубль
        elif st["status"] == "succeeded":
            if not (HERE / st.get("file", "")).exists() and st.get("url"):
                log(f"{name}: готовий, але файлу немає - завантажую ще раз")
                client.download(st["url"], out_dir / f"{name}.mp4")
            else:
                log(f"{name}: уже готовий - пропускаю")
        elif st["status"] in ("failed", "canceled", "error"):
            if args.retry_failed:
                retryable = (st.get("error") or {}).get("retryable", True)
                if not retryable:
                    log(f"{name}: відмова без права повтору ({(st.get('error') or {}).get('code')}). "
                        f"Зміни кадр або промпт, потім видали рядок {k} зі state.json")
                    continue
                state.pop(k)
                todo.append(name)
            else:
                log(f"{name}: минулого разу помилка - пропускаю (додай --retry-failed)")

    if not todo and not inflight:
        log("Нічого відправляти.")
        return
    log(f"Відправлю: {len(todo)}, чекаю: {len(inflight)}, одночасно до {parallel}, якість {res}")

    stop_submitting = False
    next_submit = 0.0
    spent = 0
    while (todo and not stop_submitting) or inflight:
        now = time.time()
        while todo and not stop_submitting and len(inflight) < parallel and now >= next_submit:
            name = todo[0]
            k = f"{name}@{res}"
            try:
                data_uri, notes = prepare_frame(name)
            except RuntimeError as e:
                log(f"{name}: пропуск - {e}")
                state[k] = {"status": "error", "error": {"code": "frame", "message": str(e), "retryable": True}}
                save_state(state)
                todo.pop(0)
                continue
            for n in notes:
                log(f"{name}: {n}")
            st = state.get(k) or {}
            idem = st.get("idem_key") or str(uuid.uuid4())
            state[k] = {"status": "submitting", "idem_key": idem}
            save_state(state)
            payload = {"model": MODEL, "input": {"prompt": scenes[name]["prompt"], "start_image_url": data_uri}}
            if res == "1080p":
                payload["input"]["resolution"] = "1080p"
            try:
                job, wait = client.submit(payload, idem)
            except Fatal as e:
                if e.status in STOP_ALL:
                    log(f"СТОП: {e}. Нові сцени не відправляю, дочекаюсь тих, що в черзі.")
                    state.pop(k, None)
                    save_state(state)
                    stop_submitting = True
                    break
                log(f"{name}: відхилено - {e}")
                state[k] = {"status": "error", "idem_key": idem, "error": dict(e.error, retryable=False)}
                save_state(state)
                todo.pop(0)
                continue
            if job is None:
                log(f"Черга повна - чекаю {wait:.0f} с")
                next_submit = now + wait
                break
            todo.pop(0)
            state[k] = {"status": job["status"], "idem_key": idem, "job_id": job["id"],
                        "cost_cents": job.get("cost_usd_cents", 0)}
            save_state(state)
            inflight[k] = {"etag": None, "next": now + POLL_SEC}
            log(f"{name}: відправлено ({job['id']}, {job.get('cost_usd_cents', 0)} ц.)")

        for k, w in list(inflight.items()):
            if w["next"] > time.time():
                continue
            st = state[k]
            job, w["etag"] = client.poll(st["job_id"], w["etag"])
            w["next"] = time.time() + POLL_SEC
            if job is None:
                continue
            name = k.split("@")[0]
            if job["status"] != st["status"]:
                st["status"] = job["status"]
                save_state(state)
                if job["status"] == "processing":
                    log(f"{name}: генерується")
            if job["status"] not in TERMINAL:
                continue
            del inflight[k]
            if job["status"] == "succeeded":
                url = (job.get("output") or {}).get("url")
                dest = out_dir / f"{name}.mp4"
                st.update(url=url, cost_cents=job.get("cost_usd_cents", 0),
                          file=str(dest.relative_to(HERE)))
                if job.get("notice"):
                    st["notice"] = job["notice"]
                    log(f"{name}: УВАГА - {job['notice'].get('code')}: {job['notice'].get('message')}")
                save_state(state)
                if url:
                    client.download(url, dest)
                spent += job.get("cost_usd_cents", 0)
                log(f"{name}: готово -> {dest.relative_to(HERE)}")
            else:
                st["error"] = job.get("error") or {"code": job["status"], "message": job["status"], "retryable": True}
                save_state(state)
                log(f"{name}: {job['status']} - {st['error'].get('code')}: {st['error'].get('message')}")

        if inflight or (todo and not stop_submitting):
            wake = [w["next"] for w in inflight.values()]
            if todo and not stop_submitting and len(inflight) < parallel:
                wake.append(next_submit)
            time.sleep(max(0.5, min(wake) - time.time()) if wake else 1)

    log(f"Готово. Витрачено цього запуску: ${spent / 100:.2f}. Перевір: python rt_veo.py status")


def main():
    ap = argparse.ArgumentParser(description="Veo 3.1 через RoyalTechno для кліпу «Ключі у траві»")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="перевірити кадри і ключ")
    sub.add_parser("price", help="баланс і ціни")
    sub.add_parser("status", help="стан усіх сцен")
    r = sub.add_parser("run", help="згенерувати кліпи")
    r.add_argument("--scenes", help="через кому: S01,S02,S03")
    r.add_argument("--all", action="store_true", help="усі сцени")
    r.add_argument("--res", choices=["720p", "1080p"], default="720p")
    r.add_argument("--parallel", type=int, default=DEFAULT_PARALLEL, help="скільки сцен одночасно (за замовч. 4)")
    r.add_argument("--retry-failed", action="store_true", help="повторити сцени з тимчасовою помилкою")
    args = ap.parse_args()
    try:
        {"check": cmd_check, "price": cmd_price, "status": cmd_status, "run": cmd_run}[args.cmd](args)
    except KeyboardInterrupt:
        log("Зупинено. Запусти ту саму команду ще раз - нічого не оплатиться двічі.")


if __name__ == "__main__":
    main()

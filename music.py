"""ACE-Step REST API による音楽生成。チャット経路とは独立して扱う。"""
import asyncio
import json
import time
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx

MUSIC_OUT = Path(__file__).resolve().parent / "music_out"
DEFAULT_BASE = "http://127.0.0.1:8001"
_generation_lock = threading.Lock()
_health_cache = {}
HEALTH_TTL = 15.0


class MusicError(Exception):
    """音楽サービスまたは入力のエラー。"""


class MusicInputError(MusicError):
    """利用者が修正できる入力エラー。"""


def _data(response):
    response.raise_for_status()
    body = response.json()
    if body.get("error") or body.get("code", 200) != 200:
        raise MusicError(f"音楽サービスがエラーを返しました: {body.get('error') or body.get('code')}")
    return body["data"]


async def music_catalog(cfg):
    models = []
    async with httpx.AsyncClient(timeout=2.0) as client:
        for key, entry in cfg.get("models", {}).items():
            if entry.get("kind") != "music":
                continue
            base = entry.get("base_url", DEFAULT_BASE).rstrip("/")
            hit = _health_cache.get(base)
            if hit is None or time.monotonic() - hit[0] >= HEALTH_TTL:
                try:
                    response = await client.get(base + "/health")
                    response.raise_for_status()
                    connected = True
                except httpx.HTTPError:
                    connected = False
                hit = (time.monotonic(), connected)
                _health_cache[base] = hit
            models.append({**entry, "key": key, "base_url": base, "connected": hit[1]})
    return {"models": models}


async def generate_music(cfg, key, prompt, lyrics=None, duration=None, seed=None,
                         instrumental=False, vocal_language=None, timeout=600):
    entry = cfg.get("models", {}).get(key, {})
    if entry.get("kind") != "music":
        raise MusicInputError(f"音楽生成モデルが見つかりません: {key}")
    if not isinstance(prompt, str) or not prompt.strip():
        raise MusicInputError("音楽の説明 prompt を指定してください")
    if duration is not None and not 10 <= duration <= 600:
        raise MusicInputError("duration は10〜600秒で指定してください")
    if timeout <= 0:
        raise MusicInputError("timeout は正の秒数で指定してください")
    base = entry.get("base_url", DEFAULT_BASE).rstrip("/")
    lm = entry.get("lm_model_path") or entry.get("lm_model")
    payload = {"prompt": prompt, "lyrics": "[Instrumental]" if instrumental else (lyrics or ""),
               "model": entry["dit_model"], "thinking": bool(lm),
               "vocal_language": vocal_language or entry.get("vocal_language") or "ja",
               "audio_duration": duration, "inference_steps": entry.get("inference_steps", 8),
               "batch_size": 1, "audio_format": "mp3", "use_format": False,
               "use_random_seed": seed is None}
    if seed is not None:
        payload["seed"] = seed
    if lm:
        payload["lm_model_path"] = lm
    if "lm_backend" in entry:
        payload["lm_backend"] = entry["lm_backend"]
    started = time.monotonic()
    acquired = False
    try:
        # threading.Lock は別イベントループからの呼び出しも直列化する。
        # 非ブロッキング取得によりキャンセル時にもロック取得が残らない。
        async with asyncio.timeout(timeout):
            while not _generation_lock.acquire(blocking=False):
                await asyncio.sleep(0.1)
            acquired = True
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=5)) as client:
                submitted = _data(await client.post(base + "/release_task", json=payload))
                task_id = submitted["task_id"]
                while True:
                    rows = _data(await client.post(base + "/query_result", json={"task_id_list": [task_id]}))
                    task = next(row for row in rows if row.get("task_id") == task_id)
                    status = int(task["status"])
                    if status == 2:
                        raise MusicError(f"音楽生成に失敗しました: {task.get('error') or task.get('result') or '原因不明'}")
                    if status == 1:
                        result = task["result"]
                        if isinstance(result, str):
                            result = json.loads(result)
                        audio = result[0] if isinstance(result, list) else result
                        remote = audio.get("file") or audio.get("path")
                        if not remote:
                            raise MusicError("音楽生成結果に音声パスがありません")
                        parts = urlsplit(remote)
                        audio_path = parse_qs(parts.query).get("path", [remote])[0]
                        response = await client.get(base + "/v1/audio", params={"path": audio_path})
                        response.raise_for_status()
                        MUSIC_OUT.mkdir(parents=True, exist_ok=True)
                        path = MUSIC_OUT / (uuid4().hex + ".mp3")
                        path.write_bytes(response.content)
                        return {"path": str(path), "file": path.name,
                                "duration": audio.get("metas", {}).get("duration", duration),
                                "elapsed_sec": round(time.monotonic() - started, 3),
                                "model": key, "seed": audio.get("seed_value", seed)}
                    await asyncio.sleep(2)
    except (TimeoutError, httpx.TimeoutException) as exc:
        raise MusicError(f"音楽生成がタイムアウトしました（上限 {timeout} 秒）") from exc
    except httpx.ConnectError as exc:
        raise MusicError(f"音楽サービスに接続できません（{base}）。ACE-Step の起動を確認してください") from exc
    except httpx.HTTPError as exc:
        raise MusicError(f"音楽サービスとの通信に失敗しました: {exc}") from exc
    except (ValueError, KeyError, TypeError, IndexError, StopIteration, AttributeError) as exc:
        raise MusicError(f"音楽サービスの応答形式が不正です: {exc}") from exc
    except OSError as exc:
        raise MusicError(f"音声ファイルの保存に失敗しました: {exc}") from exc
    finally:
        if acquired:
            _generation_lock.release()

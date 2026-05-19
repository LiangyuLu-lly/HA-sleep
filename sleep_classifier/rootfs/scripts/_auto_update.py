"""Auto poll store for target version, update, start addon."""
import asyncio, json, os, aiohttp

TOKEN = os.environ["HA_TOKEN"]
TARGET = "2.2.2"
SLUG = "0c614d55_sleep_classifier"

async def main():
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect("http://192.168.31.71:8123/api/websocket", heartbeat=30) as ws:
            await ws.receive_json()
            await ws.send_json({"type": "auth", "access_token": TOKEN})
            await ws.receive_json()
            i = 0
            # poll until store sees TARGET
            for attempt in range(20):
                i += 1
                await ws.send_json({"id": i, "type": "supervisor/api", "endpoint": f"/addons/{SLUG}/info", "method": "get"})
                r = await ws.receive_json()
                d = r.get("result", {})
                latest = d.get("version_latest", "")
                if latest == TARGET:
                    break
                i += 1
                await ws.send_json({"id": i, "type": "supervisor/api", "endpoint": "/store/reload", "method": "post"})
                await ws.receive_json()
                await asyncio.sleep(30)
            print(f"latest={latest} after {attempt+1} polls")
            if latest != TARGET:
                print("FAILED to see target version")
                return
            # update
            i += 1
            await ws.send_json({"id": i, "type": "supervisor/api", "endpoint": f"/addons/{SLUG}/update", "method": "post"})
            print("update:", json.dumps(await ws.receive_json()))
            # wait for update done
            for poll in range(30):
                await asyncio.sleep(30)
                i += 1
                await ws.send_json({"id": i, "type": "supervisor/api", "endpoint": f"/addons/{SLUG}/info", "method": "get"})
                r = await ws.receive_json()
                d = r.get("result", {})
                if d.get("version") == TARGET:
                    print(f"updated to {TARGET} after {(poll+1)*30}s")
                    break
            else:
                print("update timed out")
                return
            # start
            i += 1
            await ws.send_json({"id": i, "type": "supervisor/api", "endpoint": f"/addons/{SLUG}/start", "method": "post"})
            print("start:", json.dumps(await ws.receive_json()))
            print("ALL DONE")

asyncio.run(main())

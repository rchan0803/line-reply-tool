# -*- coding: utf-8 -*-
"""ThreadsとInstagramの投稿の数値を取ってくる。

Threads   : トークンは投稿登録スプシの config シート（threp-tool が週1で自動更新している）
Instagram : トークンは insta-auto の .env の IG_DM_TOKEN（Instagramログイン版・自動更新される方）
            ※ FacebookページのトークンはRailway側にしかなく、ローカルでは切れているため使わない
"""
import datetime, collections, re

THREADS_BASE = "https://graph.threads.net/v1.0"
IG_BASE = "https://graph.instagram.com/v23.0"
JST = datetime.timezone(datetime.timedelta(hours=9))


def _month(iso):
    d = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(JST)
    return f"{d.year}-{d.month:02d}", d


def _enough(measured, posts):
    """平均を出していいか。実測が3本以上で、その月の投稿の半分以上あること。"""
    return len(measured) >= 3 and len(measured) >= posts * 0.5


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs)) if xs else 0


# ---------- Threads ----------
def threads_metrics(token, insight_posts=30, fetch_posts=400):
    import httpx
    out = {"ok": False, "error": None, "username": None, "followers": None,
           "months": {}, "recent": [], "since": None}
    try:
        with httpx.Client(timeout=30) as c:
            me = c.get(f"{THREADS_BASE}/me",
                       params={"fields": "id,username", "access_token": token}).json()
            if "error" in me:
                out["error"] = me["error"].get("message", "")[:120]
                return out
            uid, out["username"] = me["id"], me.get("username")

            posts, url = [], f"{THREADS_BASE}/{uid}/threads"
            params = {"fields": "id,timestamp,text,permalink,media_type",
                      "limit": 100, "access_token": token}
            while url and len(posts) < fetch_posts:
                r = c.get(url, params=params).json()
                posts += r.get("data", [])
                url = (r.get("paging") or {}).get("next")
                params = None
            posts = [p for p in posts if p.get("timestamp")][:fetch_posts]

            ins = c.get(f"{THREADS_BASE}/{uid}/threads_insights",
                        params={"metric": "followers_count", "access_token": token}).json()
            for m in ins.get("data", []):
                if m.get("name") == "followers_count":
                    out["followers"] = (m.get("total_value") or {}).get("value")

            per = {}
            for p in posts[:insight_posts]:
                try:
                    d = c.get(f"{THREADS_BASE}/{p['id']}/insights",
                              params={"metric": "views,likes,replies", "access_token": token}).json()
                    vals = {}
                    for m in d.get("data", []):
                        v = m.get("values")
                        vals[m["name"]] = (v[0].get("value") if v else
                                           (m.get("total_value") or {}).get("value", 0))
                    per[p["id"]] = vals
                except Exception:
                    pass

            months = collections.defaultdict(lambda: {"posts": 0, "views": [], "likes": []})
            for p in posts:
                mk, dt = _month(p["timestamp"])
                v = per.get(p["id"], {})
                months[mk]["posts"] += 1
                if v:
                    months[mk]["views"].append(v.get("views", 0))
                    months[mk]["likes"].append(v.get("likes", 0))
            out["months"] = {k: {"posts": v["posts"],
                                 "views_avg": _avg(v["views"]) if _enough(v["views"], v["posts"]) else None,
                                 "likes_avg": _avg(v["likes"]) if _enough(v["likes"], v["posts"]) else None,
                                 "measured": len(v["views"])}
                             for k, v in months.items()}
            out["since"] = min(months) if months else None
            out["recent"] = sorted(
                [{"at": _month(p["timestamp"])[1].strftime("%m/%d %H:%M"),
                  "text": re.sub(r"\s+", " ", (p.get("text") or ""))[:46],
                  "views": per[p["id"]].get("views", 0),
                  "likes": per[p["id"]].get("likes", 0),
                  "replies": per[p["id"]].get("replies", 0),
                  "url": p.get("permalink", "")}
                 for p in posts if p["id"] in per],
                key=lambda x: -x["views"])[:8]
            out["ok"] = True
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"[:120]
    return out


# ---------- Instagram ----------
def instagram_metrics(token, insight_posts=30, fetch_posts=200):
    import httpx
    out = {"ok": False, "error": None, "username": None, "followers": None,
           "months": {}, "recent": [], "since": None}
    try:
        with httpx.Client(timeout=30) as c:
            me = c.get(f"{IG_BASE}/me",
                       params={"fields": "id,username,followers_count,media_count",
                               "access_token": token}).json()
            if "error" in me:
                out["error"] = me["error"].get("message", "")[:120]
                return out
            uid = me["id"]
            out["username"] = me.get("username")
            out["followers"] = me.get("followers_count")

            posts, url = [], f"{IG_BASE}/{uid}/media"
            params = {"fields": "id,timestamp,media_type,like_count,comments_count,caption,permalink",
                      "limit": 100, "access_token": token}
            while url and len(posts) < fetch_posts:
                r = c.get(url, params=params).json()
                posts += r.get("data", [])
                url = (r.get("paging") or {}).get("next")
                params = None
            posts = [p for p in posts if p.get("timestamp")][:fetch_posts]

            reach = {}
            for p in posts[:insight_posts]:
                try:
                    d = c.get(f"{IG_BASE}/{p['id']}/insights",
                              params={"metric": "reach", "access_token": token}).json()
                    for m in d.get("data", []):
                        v = m.get("values")
                        reach[p["id"]] = v[0].get("value") if v else None
                except Exception:
                    pass

            months = collections.defaultdict(
                lambda: {"posts": 0, "likes": [], "comments": [], "reach": []})
            for p in posts:
                mk, dt = _month(p["timestamp"])
                months[mk]["posts"] += 1
                months[mk]["likes"].append(p.get("like_count") or 0)
                months[mk]["comments"].append(p.get("comments_count") or 0)
                if p["id"] in reach and reach[p["id"]] is not None:
                    months[mk]["reach"].append(reach[p["id"]])
            out["months"] = {k: {"posts": v["posts"],
                                 "likes_avg": _avg(v["likes"]) if v["posts"] >= 3 else None,
                                 "comments_avg": _avg(v["comments"]) if v["posts"] >= 3 else None,
                                 "reach_avg": _avg(v["reach"]) if _enough(v["reach"], v["posts"]) else None,
                                 "measured": len(v["reach"])}
                             for k, v in months.items()}
            out["since"] = min(months) if months else None
            out["recent"] = sorted(
                [{"at": _month(p["timestamp"])[1].strftime("%m/%d %H:%M"),
                  "text": re.sub(r"\s+", " ", (p.get("caption") or ""))[:46],
                  "likes": p.get("like_count") or 0,
                  "comments": p.get("comments_count") or 0,
                  "reach": reach.get(p["id"]) or 0,
                  "type": {"CAROUSEL_ALBUM": "複数枚", "IMAGE": "画像", "VIDEO": "動画",
                           "REELS": "リール"}.get(p.get("media_type"), p.get("media_type") or ""),
                  "url": p.get("permalink", "")}
                 for p in posts if p["id"] in reach],
                key=lambda x: -(x["reach"] or 0))[:8]
            out["ok"] = True
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"[:120]
    return out

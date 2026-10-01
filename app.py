import csv
import hashlib
import json
import os
import re
import tempfile
import time
from datetime import date, datetime, time as dt_time, timedelta, timezone
from urllib.parse import urlparse

import pandas as pd
import requests
import streamlit as st


# ----------------------------
# Настройки и вспомогательные функции
# ----------------------------

CSV_COLUMNS = [
    "app_id",
    "app_name",
    "app_url",
    "store_country",
    "review_id",
    "author",
    "title",
    "review_text",
    "rating",
    "app_version",
    "published_at",
    "updated_at",
    "collected_at",
]

ISO_COUNTRY_CODES = set("""
ad ae af ag ai al am ao aq ar as at au aw ax az
ba bb bd be bf bg bh bi bj bm bn bo bq br bs bt bv bw by bz
ca cc cd cf cg ch ci ck cl cm cn co cr cu cv cw cx cy cz
de dj dk dm do dz
ec ee eg eh er es et
fi fj fk fm fo fr
ga gb gd ge gf gg gh gi gl gm gn gp gq gr gs gt gu gw gy
hk hm hn hr ht hu
id ie il im in io iq ir is it
je jm jo jp
ke kg kh ki km kn kp kr kw ky kz
la lb lc li lk lr ls lt lu lv ly
ma mc md me mf mg mh mk ml mm mn mo mp mq mr ms mt mu mv mw mx my mz
na nc ne nf ng ni nl no np nr nu nz
om
pa pe pf pg ph pk pl pm pn pr ps pt pw py
qa
re ro rs ru rw
sa sb sc sd se sg sh si sj sk sl sm sn so sr ss st sv sx sy sz
tc td tf tg th tj tk tl tm tn to tr tt tv tw tz
ua ug um us uy uz
va vc ve vg vi vn vu
wf ws
ye yt
za zm zw
""".split())


def parse_app_store_url(app_url: str):
    """Проверяет ссылку App Store и извлекает ID приложения и регион."""
    parsed = urlparse(app_url.strip())
    host = (parsed.hostname or "").lower()

    if parsed.scheme not in ("http", "https"):
        raise ValueError("Ссылка должна начинаться с http:// или https://.")
    if host not in ("apps.apple.com", "itunes.apple.com"):
        raise ValueError(
            "Ожидается ссылка с домена apps.apple.com или itunes.apple.com."
        )

    match = re.search(r"/id(\d+)(?:[/?#]|$)", parsed.path, flags=re.IGNORECASE)
    if not match:
        raise ValueError("В URL не найден идентификатор приложения, например id123456789.")

    app_id = match.group(1)
    path_parts = [part for part in parsed.path.split("/") if part]
    region = path_parts[0].lower() if path_parts else "us"

    if region not in ISO_COUNTRY_CODES:
        region = "us"

    return app_id, region


def normalize_countries(raw_countries: str, url_region: str):
    if not raw_countries.strip():
        return [url_region]

    result = []
    for item in raw_countries.split(","):
        code = item.strip().lower()
        if code not in ISO_COUNTRY_CODES:
            raise ValueError(f"Неизвестный код страны: {item.strip()!r}")
        if code not in result:
            result.append(code)

    if not result:
        raise ValueError("Укажите хотя бы один код страны.")

    return result


def label_value(value):
    if value is None:
        return ""
    if isinstance(value, dict):
        value = value.get("label", "")
    return str(value or "").strip()


def parse_datetime(value):
    if not value:
        return ""

    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, TypeError):
        return ""


def parse_filter_datetime(value, end_of_day=False):
    if value is None or str(value).strip() == "":
        return None

    text = str(value).strip()

    try:
        if len(text) == 10:
            d = date.fromisoformat(text)
            result = datetime.combine(d, dt_time.min, tzinfo=timezone.utc)
            if end_of_day:
                result += timedelta(days=1) - timedelta(microseconds=1)
            return result

        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError as exc:
        raise ValueError(
            f"Не удалось разобрать дату {value!r}. Используйте формат YYYY-MM-DD."
        ) from exc


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def review_identity(row):
    country = str(row.get("store_country", "") or "").strip().lower()
    review_id = str(row.get("review_id", "") or "").strip()

    if review_id:
        return f"{country}|id:{review_id}"

    fallback_fields = [
        row.get("author", ""),
        row.get("title", ""),
        row.get("review_text", ""),
        row.get("rating", ""),
        row.get("published_at", ""),
    ]
    payload = json.dumps(
        [str(x or "").strip() for x in fallback_fields],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{country}|fallback:{digest}"


def read_existing_csv(csv_path):
    if not os.path.exists(csv_path):
        return []

    try:
        df = pd.read_csv(
            csv_path,
            dtype=str,
            keep_default_na=False,
            encoding="utf-8-sig",
        )
    except (pd.errors.EmptyDataError, OSError):
        return []

    for column in CSV_COLUMNS:
        if column not in df.columns:
            df[column] = ""

    return df[CSV_COLUMNS].fillna("").to_dict(orient="records")


def write_csv_atomic(csv_path, rows):
    os.makedirs(os.path.dirname(csv_path), exist_ok=True)
    temp_path = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8-sig",
            newline="",
            delete=False,
            dir=os.path.dirname(csv_path),
            suffix=".tmp",
        ) as temp_file:
            temp_path = temp_file.name
            writer = csv.DictWriter(
                temp_file,
                fieldnames=CSV_COLUMNS,
                extrasaction="ignore",
            )
            writer.writeheader()
            for row in rows:
                writer.writerow({col: row.get(col, "") or "" for col in CSV_COLUMNS})

        os.replace(temp_path, csv_path)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)


def load_checkpoint(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError):
        return None


def save_checkpoint(path, state):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp_path = path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)
    os.replace(temp_path, path)


# ----------------------------
# Запросы к App Store
# ----------------------------

def make_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; Streamlit App Store Reviews Collector/1.0)",
        "Accept": "application/json",
    })
    return session


def request_json(
    session,
    url,
    params=None,
    timeout=25,
    max_retries=4,
    request_pause=0.4,
):
    last_error = None

    for attempt in range(max_retries + 1):
        if request_pause > 0:
            time.sleep(request_pause)

        try:
            response = session.get(url, params=params, timeout=timeout)

            if response.status_code == 429 or 500 <= response.status_code <= 599:
                if attempt >= max_retries:
                    response.raise_for_status()

                retry_after = response.headers.get("Retry-After", "")
                try:
                    delay = float(retry_after)
                except (TypeError, ValueError):
                    delay = min(2 ** attempt, 30)

                time.sleep(max(0.5, min(delay, 60)))
                continue

            response.raise_for_status()

            try:
                return response.json()
            except ValueError as exc:
                raise RuntimeError("Сервер вернул ответ не в JSON-формате.") from exc

        except requests.exceptions.HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            if status in (429,) or (status is not None and 500 <= status <= 599):
                last_error = exc
                if attempt < max_retries:
                    time.sleep(min(2 ** attempt, 30))
                    continue
            raise RuntimeError(f"HTTP-ошибка {status}: {exc}") from exc

        except (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
        ) as exc:
            last_error = exc
            if attempt < max_retries:
                time.sleep(min(2 ** attempt, 30))
                continue
            raise RuntimeError(
                f"Сетевая ошибка после {max_retries + 1} попыток: {exc}"
            ) from exc

    raise RuntimeError(f"Запрос не выполнен: {last_error}")


def lookup_app_name(session, app_id, country, timeout, max_retries, request_pause):
    data = request_json(
        session,
        "https://itunes.apple.com/lookup",
        params={"id": app_id, "country": country},
        timeout=timeout,
        max_retries=max_retries,
        request_pause=request_pause,
    )

    if isinstance(data, dict):
        results = data.get("results", [])
        if isinstance(results, list) and results and isinstance(results[0], dict):
            return str(
                results[0].get("trackName")
                or results[0].get("collectionName")
                or ""
            ).strip()

    return ""


def parse_review_feed(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("feed"), dict):
        raise RuntimeError("Неожиданный формат ответа: поле feed отсутствует.")

    feed = payload["feed"]
    entries = feed.get("entry", [])

    if isinstance(entries, dict):
        entries = [entries]
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        raise RuntimeError("Неожиданный формат ответа: feed.entry не является списком.")

    reviews = []

    for entry in entries:
        if not isinstance(entry, dict):
            continue

        rating = label_value(entry.get("im:rating"))
        content = label_value(entry.get("content"))
        title = label_value(entry.get("title"))

        if not any(key in entry for key in ("im:rating", "content", "title", "author")):
            continue
        if not (rating or content or title):
            continue

        author_obj = entry.get("author", {})
        if isinstance(author_obj, dict):
            author = label_value(author_obj.get("name"))
        else:
            author = label_value(author_obj)

        version = label_value(entry.get("im:version")) or label_value(
            entry.get("version")
        )

        reviews.append({
            "review_id": label_value(entry.get("id")),
            "author": author,
            "title": title,
            "review_text": content,
            "rating": rating,
            "app_version": version,
            "published_at": parse_datetime(label_value(entry.get("published"))),
            "updated_at": parse_datetime(label_value(entry.get("updated"))),
        })

    return reviews, feed


def get_next_page(feed, current_page, max_pages):
    links = feed.get("link", [])
    if isinstance(links, dict):
        links = [links]

    if isinstance(links, list):
        for link in links:
            if not isinstance(link, dict):
                continue

            attributes = link.get("attributes", {})
            if not isinstance(attributes, dict):
                continue

            if str(attributes.get("rel", "")).lower() == "next":
                href = str(attributes.get("href", ""))
                match = re.search(r"(?:[?&]|/)page=(\d+)", href)
                if match:
                    page_num = int(match.group(1))
                    return page_num if page_num > current_page else None
                return current_page + 1

    return current_page + 1 if current_page < max_pages else None


def review_page_url(country, app_id, page):
    return (
        f"https://itunes.apple.com/{country}/rss/customerreviews/"
        f"page={page}/id={app_id}/sortby=mostrecent/json"
    )


def matches_filters(row, date_from, date_to, ratings, versions):
    if ratings is not None:
        try:
            if int(float(row.get("rating", ""))) not in ratings:
                return False
        except (ValueError, TypeError):
            return False

    if versions is not None and row.get("app_version", "").strip() not in versions:
        return False

    if date_from is not None or date_to is not None:
        value = row.get("published_at") or row.get("updated_at")
        parsed = parse_filter_datetime(value) if value else None

        if parsed is None:
            return False
        if date_from is not None and parsed < date_from:
            return False
        if date_to is not None and parsed > date_to:
            return False

    return True


# ----------------------------
# Сбор отзывов
# ----------------------------

def collect_reviews(
    app_id,
    app_name,
    app_url,
    countries,
    max_reviews,
    date_from,
    date_to,
    ratings,
    versions,
    max_pages,
    timeout,
    max_retries,
    request_pause,
    csv_path,
    checkpoint_path,
    progress_bar,
    status_box,
):
    rows = read_existing_csv(csv_path)

    if max_reviews is not None and len(rows) > max_reviews:
        rows = rows[:max_reviews]
        write_csv_atomic(csv_path, rows)

    seen = {review_identity(row) for row in rows}
    date_from_dt = parse_filter_datetime(date_from)
    date_to_dt = parse_filter_datetime(date_to, end_of_day=True)

    filter_signature = {
        "countries": countries,
        "max_reviews": max_reviews,
        "date_from": date_from,
        "date_to": date_to,
        "ratings": sorted(ratings) if ratings is not None else None,
        "versions": sorted(versions) if versions is not None else None,
        "max_pages": max_pages,
    }
    signature_text = json.dumps(
        filter_signature,
        sort_keys=True,
        ensure_ascii=False,
    )

    saved_state = load_checkpoint(checkpoint_path)
    compatible = (
        isinstance(saved_state, dict)
        and str(saved_state.get("app_id", "")) == str(app_id)
        and saved_state.get("filter_signature") == signature_text
    )

    if compatible:
        next_pages = dict(saved_state.get("next_pages", {}))
        seen.update(saved_state.get("seen_keys", []))
        raw_total = int(saved_state.get("raw_rows_total", 0))
        duplicates_total = int(saved_state.get("duplicates_total", 0))
        statuses = dict(saved_state.get("statuses", {}))
    else:
        next_pages = {}
        raw_total = 0
        duplicates_total = 0
        statuses = {}

    for country in countries:
        next_pages.setdefault(country, 1)

    session = make_session()
    total_steps = max(1, len(countries) * max_pages)
    completed_steps = 0

    def persist():
        write_csv_atomic(csv_path, rows)
        save_checkpoint(checkpoint_path, {
            "app_id": str(app_id),
            "filter_signature": signature_text,
            "next_pages": next_pages,
            "raw_rows_total": raw_total,
            "duplicates_total": duplicates_total,
            "statuses": statuses,
            "seen_keys": sorted(seen),
            "saved_at": utc_now_iso(),
        })

    for country in countries:
        if max_reviews is not None and len(rows) >= max_reviews:
            statuses[country] = "пропущен: достигнут лимит отзывов"
            continue

        page = int(next_pages.get(country, 1))
        status_box.write(f"Собираю отзывы: регион **{country}**, страница {page}…")

        while page <= max_pages:
            if max_reviews is not None and len(rows) >= max_reviews:
                next_pages[country] = page
                statuses[country] = "остановлено: достигнут лимит отзывов"
                persist()
                break

            url = review_page_url(country, app_id, page)

            try:
                payload = request_json(
                    session,
                    url,
                    timeout=timeout,
                    max_retries=max_retries,
                    request_pause=request_pause,
                )
                reviews, feed = parse_review_feed(payload)
            except Exception as exc:
                statuses[country] = f"ошибка на странице {page}: {exc}"
                next_pages[country] = page
                persist()
                status_box.warning(f"Регион {country}, страница {page}: {exc}")
                break

            raw_total += len(reviews)

            if not reviews:
                statuses[country] = "готово: пустая страница"
                next_pages[country] = max_pages + 1
                persist()
                completed_steps += max(1, max_pages - page + 1)
                progress_bar.progress(min(completed_steps / total_steps, 1.0))
                break

            page_fully_processed = True

            for review in reviews:
                row = {
                    "app_id": str(app_id),
                    "app_name": app_name or "",
                    "app_url": app_url,
                    "store_country": country,
                    **review,
                    "collected_at": utc_now_iso(),
                }

                identity = review_identity(row)
                if identity in seen:
                    duplicates_total += 1
                    continue

                if max_reviews is not None and len(rows) >= max_reviews:
                    page_fully_processed = False
                    break

                seen.add(identity)

                if matches_filters(
                    row,
                    date_from_dt,
                    date_to_dt,
                    ratings,
                    versions,
                ):
                    rows.append(row)

                if max_reviews is not None and len(rows) >= max_reviews:
                    page_fully_processed = False
                    break

            if not page_fully_processed:
                next_pages[country] = page
                statuses[country] = "остановлено: достигнут лимит отзывов"
                persist()
                break

            next_page = get_next_page(feed, page, max_pages)

            if next_page is None or next_page > max_pages:
                statuses[country] = "готово: страниц больше нет или достигнут предел"
                next_pages[country] = max_pages + 1
                persist()
                completed_steps += max(1, max_pages - page + 1)
                progress_bar.progress(min(completed_steps / total_steps, 1.0))
                break

            next_pages[country] = next_page
            statuses[country] = "сбор продолжается"
            persist()

            completed_steps += 1
            progress_bar.progress(min(completed_steps / total_steps, 1.0))
            page = next_page

    persist()

    return {
        "rows": rows,
        "raw_rows_total": raw_total,
        "duplicates_total": duplicates_total,
        "statuses": statuses,
    }


# ----------------------------
# Интерфейс Streamlit
# ----------------------------

st.set_page_config(
    page_title="Сбор отзывов App Store",
    page_icon="⭐",
    layout="wide",
)

st.title("Сбор отзывов из App Store")
st.caption("Укажите ссылку на приложение, параметры сбора и нажмите «Начать сбор».")

with st.form("collector_form"):
    app_url = st.text_input(
        "Ссылка на приложение",
        value="https://apps.apple.com/us/app/lingard-language-learning/id1622548290",
    )

    countries_text = st.text_input(
        "Коды стран",
        value="",
        help="Например: us, gb, ca. Если оставить пустым, используется регион из ссылки.",
    )

    col1, col2, col3 = st.columns(3)
    with col1:
        max_reviews = st.number_input(
            "Максимум отзывов",
            min_value=1,
            max_value=100_000,
            value=500,
            step=100,
        )
    with col2:
        max_pages = st.number_input(
            "Максимум страниц на страну",
            min_value=1,
            max_value=100,
            value=10,
            step=1,
        )
    with col3:
        timeout = st.number_input(
            "Таймаут запроса, сек.",
            min_value=1,
            max_value=120,
            value=25,
            step=1,
        )

    date_col1, date_col2 = st.columns(2)
    with date_col1:
        date_from = st.date_input("Дата от", value=None)
    with date_col2:
        date_to = st.date_input("Дата до", value=None)

    ratings_selected = st.multiselect(
        "Оценки",
        options=[1, 2, 3, 4, 5],
        default=[],
        help="Если ничего не выбрано, попадут отзывы с любой оценкой.",
    )

    versions_text = st.text_input(
        "Версии приложения",
        value="",
        help="Необязательно. Введите версии через запятую, например: 1.2.0, 1.2.1.",
    )

    with st.expander("Дополнительные настройки"):
        retries = st.number_input(
            "Повторы при сетевых ошибках",
            min_value=0,
            max_value=10,
            value=4,
            step=1,
        )
        request_pause = st.number_input(
            "Пауза между запросами, сек.",
            min_value=0.0,
            max_value=10.0,
            value=0.4,
            step=0.1,
        )

    start = st.form_submit_button("Начать сбор", type="primary")


if start:
    try:
        app_id, url_region = parse_app_store_url(app_url)
        countries = normalize_countries(countries_text, url_region)

        if date_from and date_to and date_from > date_to:
            raise ValueError("Дата «от» не может быть позже даты «до».")

        ratings = set(ratings_selected) if ratings_selected else None
        versions = (
            {item.strip() for item in versions_text.split(",") if item.strip()}
            if versions_text.strip()
            else None
        )

        from_text = date_from.isoformat() if date_from else None
        to_text = date_to.isoformat() if date_to else None

        os.makedirs("data", exist_ok=True)
        run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        csv_path = os.path.join(
            "data",
            f"app_store_reviews_{app_id}_{run_date}.csv",
        )
        checkpoint_path = csv_path + ".checkpoint.json"

        st.write(f"**ID приложения:** `{app_id}`")
        st.write(f"**Регионы:** {', '.join(countries)}")

        session = make_session()
        app_name = ""

        try:
            app_name = lookup_app_name(
                session,
                app_id,
                countries[0],
                timeout=int(timeout),
                max_retries=int(retries),
                request_pause=float(request_pause),
            )
        except Exception as exc:
            st.warning(f"Не удалось получить название приложения через Lookup API: {exc}")

        if app_name:
            st.write(f"**Приложение:** {app_name}")

        progress_bar = st.progress(0)
        status_box = st.empty()

        result = collect_reviews(
            app_id=app_id,
            app_name=app_name,
            app_url=app_url,
            countries=countries,
            max_reviews=int(max_reviews),
            date_from=from_text,
            date_to=to_text,
            ratings=ratings,
            versions=versions,
            max_pages=int(max_pages),
            timeout=int(timeout),
            max_retries=int(retries),
            request_pause=float(request_pause),
            csv_path=csv_path,
            checkpoint_path=checkpoint_path,
            progress_bar=progress_bar,
            status_box=status_box,
        )

        df = pd.DataFrame(result["rows"], columns=CSV_COLUMNS)

        st.subheader("Результаты")
        metric1, metric2, metric3 = st.columns(3)
        metric1.metric("Уникальных отзывов", len(df))
        metric2.metric("Получено записей", result["raw_rows_total"])
        metric3.metric("Повторов", result["duplicates_total"])

        st.write("**Статусы регионов**")
        st.json(result["statuses"])

        if df.empty:
            st.info("Отзывы по заданным параметрам не найдены.")
        else:
            st.dataframe(df, use_container_width=True, hide_index=True)

            rating_counts = (
                df["rating"]
                .replace("", "Нет оценки")
                .value_counts()
                .sort_index()
            )
            st.subheader("Распределение по оценкам")
            st.bar_chart(rating_counts)

        write_csv_atomic(csv_path, result["rows"])

        csv_data = df.to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            label="Скачать CSV",
            data=csv_data,
            file_name=os.path.basename(csv_path),
            mime="text/csv",
        )

        st.caption(f"CSV также сохранён локально: `{csv_path}`")
        st.caption(f"Checkpoint для продолжения: `{checkpoint_path}`")

    except Exception as exc:
        st.error(str(exc))

import pandas as pd
import json
from pathlib import Path
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
import json
import requests
import asyncio
from crawl4ai import AsyncWebCrawler
from pathlib import Path
import trafilatura


def get_site_links(sitemap_url):
    print("Here", flush=True)
    ns = {'ns': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
    resp = requests.get(sitemap_url, timeout=10)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    urls = [url.find('ns:loc', ns).text for url in root.findall('ns:url', ns)]

    articles = []
    for url in urls:
        #print(f"Fetching {url}", flush=True)
        try:
            downloaded = trafilatura.fetch_url(url)
            if downloaded is None:
                #print(f"Skipping {url}: fetch failed", flush=True)
                continue

           
            content = trafilatura.extract(
                downloaded,
                output_format="markdown",
                include_links=False,
                include_images=False,
                favor_precision=True
            )

            if not content:
                #print(f"Skipping {url}: no extractable content", flush=True)
                continue

        
            metadata = trafilatura.extract_metadata(downloaded)

            articles.append({
                "url": url,
                "title": metadata.title if metadata else None,
                "author": metadata.author if metadata else None,
                "published_date": metadata.date if metadata else None,
                "content": content
            })

        except Exception as e:
            #print(f"Skipping {url}: {e}", flush=True)

    context_dir = Path("context")
    context_dir.mkdir(exist_ok=True)

    with open(context_dir / "gradmap_context.json", "w", encoding="utf-8") as f:
        json.dump({"articles": articles}, f, ensure_ascii=False, indent=2)

    #print(f"Saved {len(articles)} articles to context/gradmap_context.json", flush=True)


def parse_task_templates(xlsx_path):
    df = pd.read_excel(xlsx_path)
    df = df.dropna(how="all")

    df = df.rename(columns=lambda c: str(c).strip())
    df = df.fillna("")

    tasks = []
    for _, row in df.iterrows():
        task = {
            "owner": row.get("Owner", ""),
            "order": row.get("Order", ""),
            "phase": row.get("Application Phase", ""),
            "name": row.get("Name", ""),
            "requirement_type": row.get("Requirement Type", ""),
            "task_type": row.get("Task Type", ""),
            "applicability": row.get("Applicability", ""),
            "associated_requirement": row.get("Associated Requirement", ""),
            "applicable_schools": row.get("Applicable Schools", ""),
            "parent_task": row.get("Parent Task", ""),
            "required_or_recommended": row.get("Required", ""),
            "duration_min": row.get("Duration (min)", ""),
            "days_before_deadline": row.get("Calendar Days Before Deadline", ""),
            "trigger_rule": row.get("Trigger Rule", ""),
            "trigger_notes": row.get("Trigger Notes", ""),
            "when": row.get("When", ""),
            "deadline_type": row.get("Deadline Type", ""),
            "description": row.get("Description", ""),
            "links": row.get("Links", "")
        }
        
        task = {k: v for k, v in task.items() if v != ""}
        tasks.append(task)

    context_dir = Path("context")
    context_dir.mkdir(exist_ok=True)
    with open(context_dir / "triggers.json", "w", encoding="utf-8") as f:
        json.dump({"tasks": tasks}, f, ensure_ascii=False, indent=2)

    return tasks

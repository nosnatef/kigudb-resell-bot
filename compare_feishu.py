import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
import psycopg
from psycopg.rows import dict_row
from worker import FeishuFetcher, DEFAULT_SHARE_URL, cell, database_url


def main():
    load_dotenv(Path(__file__).with_name('.env'), override=False)
    if not os.environ.get('DATABASE_URL'):
        raise ValueError('DATABASE_URL is required')
    fetcher = FeishuFetcher(os.environ.get('FEISHU_SHARE_URL', DEFAULT_SHARE_URL))
    rows = fetcher.fetch()  # Always full source; never use the incremental cutoff.
    source_ids = {cell(row, 'product_id') for row in rows if re.fullmatch(r'[0-9]{5,30}', cell(row, 'product_id'))}
    if not source_ids:
        raise ValueError('Source has no valid product IDs; comparison aborted')
    with psycopg.connect(database_url(os.environ['DATABASE_URL']), row_factory=dict_row,
                         autocommit=True, connect_timeout=15) as conn:
        conn.execute('SET default_transaction_read_only = on')
        posts = conn.execute('''SELECT p.id, p.status, p.note, p."createdAt", l.link
            FROM "ResellPost" p JOIN "ResellPostLink" l ON l."resellpostId" = p.id
            WHERE p.status = 'FOR_SALE' AND l."linkType" = 'GOOFISH'
              AND left(p.id, 8) = 'goofish_'
            ORDER BY p."createdAt", p.id''').fetchall()
    active, unrecognized = {}, []
    for post in posts:
        match = re.search(r'[?&]id=([0-9]{5,30})(?:&|$)', post['link'])
        if not match:
            unrecognized.append({'postId': post['id'], 'link': post['link']})
            continue
        product_id = match[1]
        active.setdefault(post['id'], {'postId': post['id'], 'productId': product_id,
            'link': post['link'], 'title': (post['note'] or '').split('\n')[0],
            'createdAt': post['createdAt'].isoformat(), 'present': False})
        active[post['id']]['present'] |= product_id in source_ids
    missing = [post for post in active.values() if not post['present']]
    # A second complete fetch distinguishes stable absence from a changing snapshot.
    second_ids = None
    if missing:
        second = fetcher.fetch()
        second_ids = {cell(row, 'product_id') for row in second if re.fullmatch(r'[0-9]{5,30}', cell(row, 'product_id'))}
        if not second_ids:
            raise ValueError('Second source fetch has no valid product IDs; comparison aborted')
    confirmed = [post for post in missing if post['productId'] not in second_ids] if second_ids is not None else []
    report = {'checkedAt': datetime.now(timezone.utc).isoformat(), 'mode': 'read-only',
        'sourceRows': len(rows), 'sourceProducts': len(source_ids),
        'secondFetchProducts': len(second_ids) if second_ids is not None else None,
        'activeImportedPosts': len(active), 'presentInSource': sum(p['present'] for p in active.values()),
        'missingFirstFetch': len(missing), 'missingBothFetches': len(confirmed),
        'unrecognizedLinks': unrecognized, 'missingPosts': confirmed}
    output = Path('reports/feishu-availability.json')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in ('missingPosts', 'unrecognizedLinks')}))
    print('Report: ' + str(output))

if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('Comparison failed (' + type(error).__name__ + '); no database changes made.')
        raise SystemExit(1)

"""Launch a temporary application and verify the real browser workflow."""
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen
from playwright.sync_api import sync_playwright, expect

root = Path(__file__).resolve().parents[1]
with tempfile.TemporaryDirectory(prefix='analytics-browser-') as folder:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env = {**os.environ, 'DATABASE_URL': f'sqlite:///{folder}/test.db', 'SETUP_TOKEN': 'browser-test-setup'}
    with open(Path(folder)/'server.log', 'w+') as log:
        process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', str(port)], cwd=root, env=env, stdout=log, stderr=log)
        try:
            base = f'http://127.0.0.1:{port}'
            for _ in range(100):
                try:
                    urlopen(base+'/api/health', timeout=1).close()
                    break
                except OSError:
                    if process.poll() is not None:
                        log.seek(0)
                        raise RuntimeError(log.read())
                    time.sleep(.1)
            with sync_playwright() as pw:
                browser = pw.chromium.launch()
                page = browser.new_page(viewport={'width':1280,'height':1000})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(base)
                page.locator('summary').click()
                page.locator('#setup [name=setup_token]').fill('browser-test-setup')
                page.locator('#setup [name=username]').fill('admin')
                page.locator('#setup [name=password]').fill('browser-password-123')
                page.locator('#setup button').click()
                expect(page.locator('#notice')).to_contain_text('初始化完成')
                page.locator('#login [name=username]').fill('admin')
                page.locator('#login [name=password]').fill('browser-password-123')
                page.locator('#login button').click()
                expect(page.locator('#workspace')).to_be_visible()
                page.locator('#demo').click()
                expect(page.locator('#source option')).to_have_count(1)
                page.locator('#query button').click()
                expect(page.locator('#plan')).to_contain_text('支付金额')
                page.locator('#execute').click()
                expect(page.locator('#result')).to_contain_text('300.00')
                expect(page.locator('#history tr')).to_have_count(1)
                page.locator('#import [name=name]').fill('CSV 导入')
                page.locator('#import [name=file]').set_input_files(root/'examples/orders.csv')
                page.locator('#import [name=refund_file]').set_input_files(root/'examples/refunds.csv')
                page.locator('#import button').click()
                expect(page.locator('#source option')).to_have_count(2)
                expect(page.locator('#history')).to_contain_text('演示订单')
                page.locator('#source').select_option(label='CSV 导入')
                page.locator('#query [name=metric]').select_option('refund_amount')
                page.locator('#query [name=start_date]').fill('2026-02-01')
                page.locator('#query [name=end_date]').fill('2026-02-01')
                page.locator('#query button').click()
                expect(page.locator('#plan')).to_contain_text('退款发生日')
                page.locator('#execute').click()
                expect(page.locator('#result')).to_contain_text('39.99')
                page.locator('#audit-refresh').click()
                expect(page.locator('#audit tr').first).to_be_visible()

                # Seed a second page through the API, then page through the actual table.
                login = page.request.post(base + '/api/login', data={
                    'username': 'admin', 'password': 'browser-password-123',
                })
                assert login.ok, login.text()
                headers = {'Authorization': 'Bearer ' + login.json()['access_token']}
                for _ in range(20):
                    plan = page.request.post(base + '/api/previews', headers=headers, data={
                        'source_id': 1, 'metric': 'paid_orders',
                        'start_date': '2026-01-01', 'end_date': '2026-01-31',
                    })
                    assert plan.ok, plan.text()
                    result = page.request.post(
                        base + '/api/previews/' + plan.json()['id'] + '/execute', headers=headers,
                    )
                    assert result.ok, result.text()
                page.locator('#refresh').click()
                expect(page.locator('#history tr')).to_have_count(20)
                page.locator('#history-more').click()
                expect(page.locator('#history tr')).to_have_count(22)
                expect(page.locator('#history-more')).to_be_hidden()
                expect(page.locator('#history tr').last).to_contain_text('300.00')
                (root/'artifacts').mkdir(exist_ok=True)
                page.screenshot(path=root/'artifacts/workbench.png', full_page=True)
                page.set_viewport_size({'width':390,'height':844})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.locator('#logout').click()
                expect(page.locator('#auth')).to_be_visible()
                assert not errors, errors
                browser.close()
                print('PASS browser: setup, login, demo, preview, execute, history pagination, audit, multi-day refund CSV import, mobile layout, logout')
        finally:
            process.terminate()
            process.wait(timeout=10)

# تطبيق Flask لمشروع ملمس — صفحة حجز هدية العقيقة (Smoke Test)
# يُغطّي: الصفحة الرئيسية، صفحة الهبوط، استلام الحجوزات،
# ولوحة إدارة محميّة بكلمة مرور (HTTP Basic Auth) لعرض الحجوزات.

import json
import os
import re
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime
from functools import wraps

from flask import (
    Flask, render_template, request, jsonify,
    Response, send_from_directory,
)
from werkzeug.middleware.proxy_fix import ProxyFix

# تحميل ملفّ .env محلّياً (يُتجاهل بصمت إن لم يكن مثبّتاً في الإنتاج).
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ----------------------------------------------------------------------
# إعدادات التطبيق
# ----------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-only-change-in-production')

# Render يضع التطبيق خلف reverse proxy — نطلب من Flask احترام X-Forwarded-Proto
# حتى يولّد روابط https:// عند الحاجة (مفيد للمستقبل وللخلف الصحيح للوكيل).
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_for=1)
if os.environ.get('RENDER'):
    app.config['PREFERRED_URL_SCHEME'] = 'https'

# مسار قاعدة البيانات بصيغة مطلقة — يعمل محلّياً وعلى Render معاً.
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data.db')

# كلمة مرور لوحة الإدارة — تُضبط في Render dashboard بمتغيّر البيئة ADMIN_PASSWORD.
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '')

# مفتاح Anthropic — يُضبط في Render dashboard بمتغيّر البيئة ANTHROPIC_API_KEY.
# يُستخدم لتوكيل نداءات الـ miniApps إلى Claude بحيث يبقى المفتاح على الخادم.
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
ANTHROPIC_DEFAULT_MODEL = os.environ.get(
    'ANTHROPIC_MODEL', 'claude-sonnet-4-5-20250929'
)
MINIAPPS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'miniApps')


# ----------------------------------------------------------------------
# قاعدة البيانات — تهيئة الجداول وعمليّات القراءة/الكتابة
# ----------------------------------------------------------------------

# تُنشئ جدول الحجوزات في قاعدة البيانات إن لم يكن موجوداً.
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS bookings (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name       TEXT    NOT NULL,
                phone           TEXT    NOT NULL,
                region          TEXT    NOT NULL,
                payment_method  TEXT    NOT NULL,
                deposit_status  TEXT    NOT NULL DEFAULT 'reserved',
                created_at      TEXT    NOT NULL
            )
        ''')
        conn.commit()


# تتحقّق من صحّة رقم الجوّال السعوديّ (يبدأ بـ5 ويتكوّن من 9 خانات بعد المفتاح الدوليّ).
def is_valid_saudi_phone(phone):
    cleaned = re.sub(r'[\s\-+]', '', phone or '')
    if cleaned.startswith('966'):
        cleaned = cleaned[3:]
    if cleaned.startswith('0'):
        cleaned = cleaned[1:]
    return bool(re.fullmatch(r'5\d{8}', cleaned))


# تحفظ بيانات الحجز في جدول bookings وتُعيد المُعرّف الجديد.
def save_booking(full_name, phone, region, payment_method):
    created_at = datetime.utcnow().isoformat()
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute(
            '''INSERT INTO bookings
               (full_name, phone, region, payment_method, deposit_status, created_at)
               VALUES (?, ?, ?, ?, ?, ?)''',
            (full_name, phone, region, payment_method, 'reserved', created_at)
        )
        conn.commit()
        return cursor.lastrowid


# تُولّد رسالة التأكيد العربية التي تُعاد للأمّ بعد نجاح الحجز.
def build_confirmation_message(full_name):
    return (
        f'مرحباً {full_name}، شكراً لحجزكِ هديّة العقيقة من ملمس. '
        'سنتواصل معكِ خلال 4 إلى 6 أسابيع لتأكيد الشحن أو ردّ مبلغ العربون (20 ريالاً) كاملاً.'
    )


# تُعيد جميع الحجوزات مرتّبة تنازلياً حسب تاريخ الإنشاء (للوحة الإدارة).
def list_all_bookings():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            '''SELECT id, full_name, phone, region, payment_method,
                      deposit_status, created_at
               FROM bookings
               ORDER BY datetime(created_at) DESC'''
        ).fetchall()
    return [dict(r) for r in rows]


# ----------------------------------------------------------------------
# حماية لوحة الإدارة بكلمة مرور (HTTP Basic Auth)
# ----------------------------------------------------------------------

# تتحقّق من أنّ الزائر أرسل كلمة المرور الصحيحة في رأس Authorization.
def is_admin_authenticated():
    if not ADMIN_PASSWORD:
        return False
    auth = request.authorization
    if not auth or not auth.password:
        return False
    return auth.password == ADMIN_PASSWORD


# مزخرف يحمي أيّ Route من الزوّار غير المصادقين — يُظهر نافذة الدخول الأصليّة.
def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not is_admin_authenticated():
            return Response(
                'يلزم إدخال كلمة المرور للوصول إلى لوحة الإدارة.',
                401,
                {'WWW-Authenticate': 'Basic realm="ملمس · لوحة الإدارة"'},
            )
        return view(*args, **kwargs)
    return wrapper


# ----------------------------------------------------------------------
# المسارات الأساسية — الصفحة الرئيسية وصفحة الهبوط
# ----------------------------------------------------------------------

# المسار الرئيسي: يعرض صفحة العرض ونموذج الحجز للأمّ السعوديّة.
@app.route('/')
def index():
    try:
        return render_template('index.html')
    except Exception as error:
        return jsonify({
            'success': False,
            'message': 'تعذّر عرض الصفحة الرئيسيّة. يُرجى المحاولة لاحقاً.',
            'error_detail': str(error)
        }), 500


# صفحة الهبوط (Landing Page): تشرح المنتج لمن لم يسمع بـ ملمس قبلاً.
@app.route('/about')
def about():
    return render_template('landing.html')


# مسار المعالجة: ينفّذ خطوات Process من الذاكرة (تحقّق + حفظ + تأكيد) ويُعيد JSON.
@app.route('/process', methods=['POST'])
def process_booking():
    try:
        # الخطوة 1: استلام بيانات النموذج وتنظيفها.
        full_name = (request.form.get('full_name') or '').strip()
        phone = (request.form.get('phone') or '').strip()
        region = (request.form.get('region') or '').strip()
        payment_method = (request.form.get('payment_method') or '').strip()

        # الخطوة 2: التحقّق من اكتمال الحقول.
        if not all([full_name, phone, region, payment_method]):
            return jsonify({
                'success': False,
                'message': 'الرجاء تعبئة جميع الحقول المطلوبة قبل المتابعة.'
            }), 400

        # الخطوة 3: التحقّق من صحّة رقم الجوّال السعوديّ.
        if not is_valid_saudi_phone(phone):
            return jsonify({
                'success': False,
                'message': 'رقم الجوّال غير صحيح. يجب أن يبدأ بـ5 ويتكوّن من 9 خانات.'
            }), 400

        # الخطوة 4: حفظ الحجز في قاعدة البيانات.
        booking_id = save_booking(full_name, phone, region, payment_method)

        # الخطوة 5: توليد رسالة التأكيد وإرجاع الاستجابة بصيغة JSON.
        confirmation = build_confirmation_message(full_name)
        return jsonify({
            'success': True,
            'booking_id': booking_id,
            'deposit_amount_sar': 20,
            'message': confirmation
        }), 201

    except Exception as error:
        return jsonify({
            'success': False,
            'message': 'حدث خطأ غير متوقّع أثناء معالجة الحجز. يُرجى المحاولة مجدّداً.',
            'error_detail': str(error)
        }), 500


# ----------------------------------------------------------------------
# لوحة الإدارة — محميّة بكلمة مرور
# ----------------------------------------------------------------------

# تعرض جدول جميع الحجوزات — للناشر فقط بعد إدخال كلمة المرور.
@app.route('/admin')
@admin_required
def admin_panel():
    bookings = list_all_bookings()
    return render_template('admin.html', bookings=bookings)


# ----------------------------------------------------------------------
# miniApps — أدوات داخلية للفريق (محميّة بكلمة مرور الإدارة)
# ----------------------------------------------------------------------

# لوحة الأدوات الموحّدة — تجمع كل miniApps في واجهة واحدة.
@app.route('/dashboard')
@admin_required
def miniapps_dashboard():
    return send_from_directory(MINIAPPS_DIR, 'malmas_dashboard.html')


# يقدّم ملفّات HTML من مجلد miniApps. مثال: /miniapps/malmas_caption_generator.html
@app.route('/miniapps/<path:filename>')
@admin_required
def miniapps_static(filename):
    # السماح فقط بملفّات .html داخل المجلد — حماية ضدّ Path Traversal.
    if not filename.endswith('.html') or '..' in filename or '/' in filename:
        return Response('غير مسموح.', 403)
    return send_from_directory(MINIAPPS_DIR, filename)


# توكيل آمن لنداءات Claude — يُبقي ANTHROPIC_API_KEY على الخادم.
# يستقبل { prompt, max_tokens?, model? } ويعيد { text } أو { error }.
@app.route('/api/generate', methods=['POST'])
@admin_required
def api_generate():
    if not ANTHROPIC_API_KEY:
        return jsonify({
            'error': 'ANTHROPIC_API_KEY غير مضبوط في البيئة. اضبطه على Render قبل استخدام أدوات التوليد.'
        }), 503

    payload = request.get_json(silent=True) or {}
    prompt = (payload.get('prompt') or '').strip()
    if not prompt:
        return jsonify({'error': 'الحقل "prompt" مطلوب.'}), 400

    max_tokens = int(payload.get('max_tokens') or 2400)
    if max_tokens < 64 or max_tokens > 8000:
        max_tokens = 2400
    model = payload.get('model') or ANTHROPIC_DEFAULT_MODEL

    body = json.dumps({
        'model': model,
        'max_tokens': max_tokens,
        'messages': [{'role': 'user', 'content': prompt}],
    }).encode('utf-8')

    req = urllib.request.Request(
        'https://api.anthropic.com/v1/messages',
        data=body,
        method='POST',
        headers={
            'Content-Type': 'application/json',
            'x-api-key': ANTHROPIC_API_KEY,
            'anthropic-version': '2023-06-01',
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as http_err:
        detail = ''
        try:
            detail = http_err.read().decode('utf-8')[:400]
        except Exception:
            pass
        return jsonify({
            'error': f'فشل نداء Claude: HTTP {http_err.code}',
            'detail': detail,
        }), 502
    except urllib.error.URLError as url_err:
        return jsonify({
            'error': 'تعذّر الاتصال بـ Claude — تحقّق من الاتصال.',
            'detail': str(url_err.reason),
        }), 502
    except Exception as error:
        return jsonify({
            'error': 'خطأ غير متوقّع أثناء التوليد.',
            'detail': str(error),
        }), 500

    # تجميع النصّ من كتل الاستجابة.
    text_parts = []
    for block in data.get('content', []) or []:
        if block.get('type') == 'text':
            text_parts.append(block.get('text', ''))
    return jsonify({
        'text': '\n'.join(text_parts).strip(),
        'model': data.get('model'),
        'usage': data.get('usage'),
    })


# ----------------------------------------------------------------------
# نقطة الانطلاق
# ----------------------------------------------------------------------

# يُهيّئ قاعدة البيانات عند الاستيراد (مفيد لـ gunicorn) وعند التشغيل المباشر.
init_db()


if __name__ == '__main__':
    # تشغيل محلّي على PORT من البيئة (Render يضعه تلقائياً) أو 5000 افتراضياً.
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', '').lower() in ('1', 'true', 'yes')
    app.run(host='0.0.0.0', port=port, debug=debug)

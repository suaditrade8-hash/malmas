# تطبيق Flask لمشروع ملمس — صفحة حجز هدية العقيقة (Smoke Test)
# يُغطّي: الصفحة الرئيسية، صفحة الهبوط، تسجيل الدخول بـ Google OAuth،
# لوحة الإدارة (Admin Panel)، وإرسال الحجوزات إلى قاعدة بيانات SQLite.

import os
import re
import sqlite3
from datetime import datetime
from functools import wraps

from flask import (
    Flask, render_template, request, jsonify,
    session, redirect, url_for, abort,
)
from werkzeug.middleware.proxy_fix import ProxyFix
from authlib.integrations.flask_client import OAuth

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
# حتى يولّد روابط https:// (مطلوبة لمطابقة redirect URI في Google OAuth).
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_for=1)
if os.environ.get('RENDER'):
    app.config['PREFERRED_URL_SCHEME'] = 'https'

# مسار قاعدة البيانات بصيغة مطلقة — يعمل محلّياً وعلى Render معاً.
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data.db')


# قائمة بريد المدراء من المتغيّرات السرّية (مفصولة بفواصل).
def get_admin_emails():
    raw = os.environ.get('ADMIN_EMAILS', '')
    return {e.strip().lower() for e in raw.split(',') if e.strip()}


# ----------------------------------------------------------------------
# تسجيل عميل Google OAuth
# ----------------------------------------------------------------------

oauth = OAuth(app)
oauth.register(
    name='google',
    client_id=os.environ.get('GOOGLE_CLIENT_ID'),
    client_secret=os.environ.get('GOOGLE_CLIENT_SECRET'),
    server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
    client_kwargs={'scope': 'openid email profile'},
)


# ----------------------------------------------------------------------
# قاعدة البيانات — تهيئة الجداول وعمليّات القراءة/الكتابة
# ----------------------------------------------------------------------

# تُنشئ جداول الحجوزات والمستخدمين عند أوّل تشغيل إن لم تكن موجودة.
def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        # جدول حجوزات هدية العقيقة (موجود من اليوم الثالث).
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

        # جدول المستخدمين — يُنشأ في اليوم الرابع لتسجيل الدخول بـ Google.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id          INTEGER  PRIMARY KEY AUTOINCREMENT,
                email       TEXT     UNIQUE NOT NULL,
                name        TEXT,
                picture_url TEXT,
                role        TEXT     NOT NULL DEFAULT 'user',
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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


# تُسجّل المستخدم في جدول users (إدراج جديد أو تحديث بياناته)، وتُعيد قاموساً يحوي الدور.
def upsert_user(email, name, picture_url):
    email_lower = (email or '').strip().lower()
    admin_emails = get_admin_emails()
    role = 'admin' if email_lower in admin_emails else 'user'

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        existing = conn.execute(
            'SELECT id, role FROM users WHERE email = ?', (email_lower,)
        ).fetchone()

        if existing:
            # تحديث الاسم والصورة فقط — لا نُلغي ترقية الأدمن إن وُجدت.
            new_role = 'admin' if email_lower in admin_emails else existing['role']
            conn.execute(
                '''UPDATE users
                   SET name = ?, picture_url = ?, role = ?
                   WHERE id = ?''',
                (name, picture_url, new_role, existing['id'])
            )
            user_id = existing['id']
            role = new_role
        else:
            cursor = conn.execute(
                '''INSERT INTO users (email, name, picture_url, role)
                   VALUES (?, ?, ?, ?)''',
                (email_lower, name, picture_url, role)
            )
            user_id = cursor.lastrowid

        conn.commit()

    return {
        'id': user_id,
        'email': email_lower,
        'name': name,
        'picture': picture_url,
        'role': role,
    }


# تُعيد جميع المستخدمين مرتّبين تنازلياً حسب تاريخ التسجيل (للوحة الإدارة).
def list_all_users():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            '''SELECT id, email, name, picture_url, role, created_at
               FROM users
               ORDER BY datetime(created_at) DESC'''
        ).fetchall()
    return [dict(r) for r in rows]


# ----------------------------------------------------------------------
# مزخرفات الحماية — login_required و admin_required
# ----------------------------------------------------------------------

# تُلزم الزائر بتسجيل الدخول قبل الوصول لأيّ Route محميّ.
def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get('user'):
            return redirect(url_for('login'))
        return view(*args, **kwargs)
    return wrapper


# تُلزم بدور admin — وإلّا تُعيد 403 برسالة عربية مفهومة.
def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        user = session.get('user')
        if not user:
            return redirect(url_for('login'))
        if user.get('role') != 'admin':
            return render_template('403.html'), 403
        return view(*args, **kwargs)
    return wrapper


# يُتاح متغيّر `current_user` تلقائياً في كلّ القوالب (templates).
@app.context_processor
def inject_user():
    return {'current_user': session.get('user')}


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
# مسارات تسجيل الدخول بـ Google OAuth
# ----------------------------------------------------------------------

# يُحوّل الزائر إلى صفحة Google لتسجيل الدخول بحساب موحّد.
@app.route('/login')
def login():
    redirect_uri = url_for('callback', _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


# المسار الذي يعود إليه Google بعد موافقة المستخدم — يُنشئ الجلسة.
@app.route('/auth/callback')
def callback():
    try:
        token = oauth.google.authorize_access_token()
        userinfo = token.get('userinfo') or oauth.google.userinfo()
        if not userinfo or not userinfo.get('email'):
            return render_template('403.html'), 400

        user = upsert_user(
            email=userinfo['email'],
            name=userinfo.get('name', ''),
            picture_url=userinfo.get('picture', ''),
        )
        session['user'] = user
        return redirect(url_for('index'))

    except Exception as error:
        return jsonify({
            'success': False,
            'message': 'تعذّر تسجيل الدخول عبر Google. يُرجى المحاولة لاحقاً.',
            'error_detail': str(error),
        }), 500


# يُلغي الجلسة ويُعيد الزائر للصفحة الرئيسية.
@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))


# ----------------------------------------------------------------------
# لوحة الإدارة — للمدراء فقط
# ----------------------------------------------------------------------

# تعرض جدول جميع المستخدمين المسجّلين — للمدراء حصراً.
@app.route('/admin')
@admin_required
def admin_panel():
    users = list_all_users()
    return render_template('admin.html', users=users)


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

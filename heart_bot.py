import json
import time
from threading import Thread
from flask import Flask, request, render_template, abort, jsonify
from config import *
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from medsenger_api import *
from mail_api import *
import pytesseract
from pdf2image import convert_from_path
from PIL import Image
import os

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1000 * 1000
db_string = "postgresql://{}:{}@{}:{}/{}".format(DB_LOGIN, DB_PASSWORD, DB_HOST, DB_PORT, DB_DATABASE)
app.config['SQLALCHEMY_DATABASE_URI'] = db_string
db = SQLAlchemy(app)

medsenger_api = AgentApiClient(APP_KEY, MAIN_HOST, debug=True)


class Params(db.Model):
    name = db.Column(db.String, primary_key=True)
    value = db.Column(db.String, nullable=True)


class Contracts(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    active = db.Column(db.Boolean, default=True)
    code = db.Column(db.String, nullable=True)
    email = db.Column(db.String, nullable=True)


try:
    db.create_all()

    query = Params.query.filter_by(name='last_id')
    if query.count() == 0:
        param = Params(name='last_id', value='-1')
        db.session.add(param)
        db.session.commit()

except:
    print('cant create structure')


def send_init_message(contract):
    contract_id = contract.id

    agent_token = medsenger_api.get_agent_token(contract_id)
    info = medsenger_api.get_patient_info(contract_id)

    link = f"https://heart.medsenger.ru/app/?contract_id={contract.id}&agent_token={agent_token['agent_token']}&" \
           f"birthdate={info['birthday']}&firstName={info['name'].split()[1]}&lastName={info['name'].split()[0]}&" \
           f"gender={info['sex']}"

    medsenger_api.send_message(contract_id,
                               'Если у вас есть карманный монитор сердечного ритма "Сердечко", измерения ЭКГ '
                               'могут автоматически поступать врачу. Для этого Вам нужно скачать приложение '
                               '<strong>ECG mob</strong>, а затем нажать на кнопку "Подключить сердечко" ниже.',
                               action_link=link, action_type='url', action_name='Подключить сердечко')


def gts():
    now = datetime.now()
    return now.strftime("%Y-%m-%d %H:%M:%S")


@app.route('/status', methods=['POST'])
def status():
    data = request.json

    if data['api_key'] != APP_KEY:
        return 'invalid key'

    contract_ids = [l[0] for l in db.session.query(Contracts.id).filter_by(active=True).all()]

    answer = {
        "is_tracking_data": True,
        "supported_scenarios": ['heartfailure', 'stenocardia', 'fibrillation'],
        "tracked_contracts": contract_ids
    }

    return json.dumps(answer)


@app.route('/init', methods=['POST'])
def init():
    data = request.json

    if data['api_key'] != APP_KEY:
        return 'invalid key'

    try:
        contract_id = int(data['contract_id'])
        query = Contracts.query.filter_by(id=contract_id)
        if query.count() != 0:
            contract = query.first()
            contract.active = True

            if data.get('params'):
                code = data['params'].get('heart_device_code')
                email = data['params'].get('heart_device_email')
            else:
                code = None
                email = None

            if code:
                contract.code = code

            if email:
                contract.email = email
            else:
                contract.email = f'cardio+{contract_id}@medsenger.ru'

            print("{}: Reactivate contract {}".format(gts(), contract.id))
        else:
            contract = Contracts(id=contract_id)

            if data.get('params'):
                code = data['params'].get('heart_device_code')
                email = data['params'].get('heart_device_email')
            else:
                code = None
                email = None
            if code:
                contract.code = code

            if email:
                contract.email = email
            else:
                contract.email = f'cardio+{contract_id}@medsenger.ru'

            db.session.add(contract)

            print("{}: Add contract {}".format(gts(), contract.id))

        send_init_message(contract)
        db.session.commit()
        medsenger_api.add_record(contract_id, 'doctor_action',
                                 f'Подключен прибор "Сердечко" {contract.code} / {contract.email}.')

    except Exception as e:
        print(e)
        return "error"
    return 'ok'


@app.route('/remove', methods=['POST'])
def remove():
    data = request.json

    if data['api_key'] != APP_KEY:
        print('invalid key')
        return 'invalid key'

    try:
        contract_id = str(data['contract_id'])
        query = Contracts.query.filter_by(id=contract_id)

        if query.count() != 0:
            contract = query.first()
            contract.active = False
            db.session.commit()

            medsenger_api.add_record(data.get('contract_id'), 'doctor_action',
                                     f'Отключен прибор "Сердечко" ({contract.code} / {contract.email}).')

            print("{}: Deactivate contract {}".format(gts(), contract.id))
        else:
            print('contract not found')

    except Exception as e:
        print(e)
        return "error"

    return 'ok'


@app.route('/order', methods=['POST'])
def order():
    data = request.json

    if data['order'] == 'heart_request_ecg':
        if data['api_key'] != APP_KEY:
            print('invalid key')
            return 'invalid key'

        try:
            contract_id = str(data['contract_id'])
            query = Contracts.query.filter_by(id=contract_id)

            if query.count() != 0:
                agent_token = medsenger_api.get_agent_token(contract_id)
                info = medsenger_api.get_patient_info(contract_id)

                link = f"https://heart.medsenger.ru/app/?contract_id={contract_id}&agent_token={agent_token['agent_token']}&birthdate={info['birthday']}&firstName={info['name'].split()[1]}&lastName={info['name'].split()[0]}&gender={info['sex']}"
                medsenger_api.send_message(contract_id,
                                           "Пожалуйста, сделайте ЭКГ с помощью сердечка в приложении EcgMob и отправьте результат врачу.",
                                           link, "Сделать ЭКГ", only_patient=True, action_type="url")
                return 'ok'
            else:
                print('contract not found')


        except Exception as e:
            print(e)
        return "error"

    return "not supported"


@app.route('/settings', methods=['GET'])
def settings():
    key = request.args.get('api_key', '')

    if key != APP_KEY:
        return "<strong>Некорректный ключ доступа.</strong> Свяжитесь с технической поддержкой."

    try:
        contract_id = int(request.args.get('contract_id'))
        query = Contracts.query.filter_by(id=contract_id)
        if query.count() != 0:
            contract = query.first()
            send_init_message(contract)
        else:
            return "<strong>Ошибка. Контракт не найден.</strong> Попробуйте отключить и снова подключить " \
                   "интеллектуальный агент к каналу консультирования. Если это не сработает, свяжитесь с технической " \
                   "поддержкой."

    except Exception as e:
        print(e)
        return "error"

    return render_template('settings.html', contract=contract)


@app.route('/settings', methods=['POST'])
def setting_save():
    key = request.args.get('api_key', '')

    if key != APP_KEY:
        return "<strong>Некорректный ключ доступа.</strong> Свяжитесь с технической поддержкой."

    try:
        contract_id = int(request.args.get('contract_id'))
        query = Contracts.query.filter_by(id=contract_id)
        if query.count() != 0:
            contract = query.first()
            contract.code = request.form.get('code')
            contract.email = request.form.get('email')
            db.session.commit()
        else:
            return "<strong>Ошибка. Контракт не найден.</strong> Попробуйте отключить и снова подключить " \
                   "интеллектуальный агент к каналу консультирования. Если это не сработает, свяжитесь с технической " \
                   "поддержкой."

    except Exception as e:
        print(e)
        return "error"

    return """
    <strong>Спасибо, окно можно закрыть</strong><script>window.parent.postMessage('close-modal-success','*');</script>
        """


@app.route('/', methods=['GET'])
def index():
    return 'waiting for the thunder!'


def tasks():
    try:
        contracts = Contracts.query.filter_by(active=True).all()
        param = Params.query.filter_by(name='last_id').first()

        last_id, messages = get_messages(param.value)

        if last_id:
            param.value = last_id
            db.session.commit()

            for contract in contracts:
                if not contract.code:
                    continue
                for message in messages:

                    hds = decode_header(message['subject'])
                    sender, cid = extract_contract_id(message)

                    if not hds and not cid:
                        continue

                    if hds:
                        data, encoding = hds[0]
                        if encoding:
                            subject = data.decode(encoding)
                        else:
                            subject = data
                    else:
                        subject = ""

                    if contract.code in subject or int(cid) == contract.id or sender == contract.email:
                        attachments = get_attachments(message)
                        medsenger_api.send_message(contract.id, text="результаты снятия ЭКГ", attachments=attachments,
                                                   send_from='patient')

                        medsenger_api.send_message(contract.id,
                                                   'Вы прислали ЭКГ. Пожалуйста, напишите врачу, почему Вы решили '
                                                   'снять ЭКГ и какие ощущения Вы испытываете?',
                                                   only_patient=True)
    except Exception as e:
        print(e)


def sender():
    while True:
        tasks()
        time.sleep(60)


@app.route('/message', methods=['POST'])
def save_message():
    data = request.json
    key = data['api_key']

    if key != APP_KEY:
        return "<strong>Некорректный ключ доступа.</strong> Свяжитесь с технической поддержкой."

    if data.get('message', {}).get('attachments'):
        for attachment in data['message']['attachments']:
            if 'ecg_' in attachment['name']:
                medsenger_api.send_message(data['contract_id'],
                                           'Похоже, что Вы прислали ЭКГ. Пожалуйста, напишите врачу, почему Вы решили '
                                           'снять ЭКГ и какие ощущения Вы испытываете?',
                                           only_patient=True)

    return "ok"

def get_pulse_from_file(content):
    name = str(uuid.uuid4()) + '.pdf'

    with open(f'./uploaded_files/{name}', 'wb') as path:
        path.write(content)

    files = convert_from_path('./uploaded_files/' + name)

    results = []

    for file in range(len(files)):
        files[file].save(f'{str(file)}.jpg', 'JPEG')

        image = Image.open(f'{str(file)}.jpg')
        w, h = image.size
        im_crop = image.crop((w // 1.26, 70, h // 1.5, w // 2 - 715))
        text = pytesseract.image_to_string(im_crop, lang='rus')

        if text == '':
            (l, u, r, d) = (w // 2 + 550, 150, h // 1.5, w // 8.8)
            im_crop1 = image.crop((l, u, r, d))
            text = pytesseract.image_to_string(im_crop1, lang='rus')

        new_text = text.split()
        for word in new_text:
            if word.isnumeric():
                word = int(word)
                results.append(word)

    summa = 0
    kolvo = 0

    for counter in results:
        summa += counter
        kolvo += 1

    pulse = summa // kolvo
    os.remove(f"./uploaded_files/{name}")

    return pulse


@app.route('/api/receive', methods=['POST'])
def receive_ecg():
    contract_id = request.form.get('contract_id')

    if not contract_id:
        abort(422, "No contract_id")

    agent_token = request.form.get('agent_token')

    if not agent_token:
        abort(422, "No agent_token")

    answer = medsenger_api.get_agent_token(contract_id)

    if not answer or answer.get('agent_token') != agent_token:
        abort(422, "Incorrect token")

    if 'ecg' in request.files:
        file = request.files['ecg']
        filename = file.filename
        print(filename)
        data = file.read()

        if not filename or not data:
            abort(422, "No filename")
        else:
            try:
                medsenger_api.send_message(contract_id, "Результаты снятия ЭКГ c Сердечка.", send_from='patient',
                                       need_answer=True, attachments=[prepare_binary(filename, data)])
            except Exception as e:
                print("Error sending pdf:", e)


            try:
                pulse = get_pulse_from_file(data)
                medsenger_api.add_record(contract_id, "pulse", pulse)
            except Exception as e:
                print("Error extracting pulse from pdf:", e)


            return 'ok'

    else:
        abort(422, "No file")


@app.route('/api/receive', methods=['GET'])
def receive_ecg_test():
    return """
    <form method="POST" enctype="multipart/form-data">
        contract_id <input name="contract_id"><br>
        agent_token <input name="agent_token"><br>
        ecg <input name="ecg" type="file"><br>
        <button>go</button>
    </form>
    """


@app.route('/app/', methods=['GET'])
def app_page():
    return render_template('get_app.html')


@app.route('/app', methods=['GET'])
def app_page2():
    return render_template('get_app.html')


@app.route('/.well-known/apple-app-site-association')
def apple_deeplink():
    return jsonify({
        "applinks": {
            "apps": [],
            "details": [
                {
                    "appID": "TR6RHMAD2G.ru.bioss.cardio",
                    "paths": [
                        "*"
                    ]
                },
                {
                    "appID": "CRF22TKXX5.ru.bioss.cardio",
                    "paths": [
                        "*"
                    ]
                }
            ]
        }
    })


@app.route('/.well-known/assetlinks.json')
def android_deeplink():
    return jsonify([{
        "relation": ["delegate_permission/common.handle_all_urls"],
        "target": {
            "namespace": "android_app", "package_name": "ru.bioss.ecgmob",
            "sha256_cert_fingerprints": [
                "4F:56:2B:08:4C:6A:95:E9:4E:DA:96:B8:BA:8A:B5:EF:D5:3A:4C:6D:8D:B8:5E:DD:8F:76:AE:2A:B5:97:C1:E7"],
        },
    }])


if __name__ == "__main__":
    t = Thread(target=sender)
    t.start()

    app.run(port=PORT, host=HOST)

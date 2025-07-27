# 📡 ssm2mqtt

![Python](https://img.shields.io/badge/python-3.11-5da1d8)
[![License](https://img.shields.io/badge/license-MIT-5da1d8)](LICENSE)
![Platform](https://img.shields.io/badge/platform-Linux%20%2F%20Windows%20%2F%20macOS-ffb8d2)

Bluetooth Low EnergyでSesame 5デバイスとMQTTをブリッジするPythonスクリプト。

HomebridgeやHome Assistantから、BLE経由でのSesame 5の操作を可能にします。

---

## 🚀 機能

- MQTTのメッセージに応じてSesame 5をBLE経由で施錠、開錠、トグル操作。
- 施錠状態、電池残量、電池不足警告をBLE経由でリアルタイムでMQTTにパブリッシュ。

---

## 📦 対応機種

|対応状況|機種|
|:-:|:-:|
|✅|Sesame 5|
|⚠️|Sesame 5 Pro|
|⚠️|Sesame 5 USA|
|❌|Sesame 4以前|

Sesame 5 ProとSesame 5 USAは恐らく動作しますが、動作未確認です。

Sesame 4以前はOSが違うため動作しません。対応予定もありません。

---

## 🛠️ 開発環境

- Windows 11 24H2, Python 3.13.3
- Raspberry Pi Zero 2W, Raspberry Pi OS Trixie (64bit), Python 3.13.3
- Sesame 5, 3.0-5-18a8e4

## 📚 使用ライブラリ

- [aiomqtt](https://github.com/empicano/aiomqtt)
- [gomalock](https://github.com/meronepy/gomalock)

---

## ⚠️ 注意事項

- Linuxで動作させる場合、 **BlueZ 5.82以降を強く推奨します。** Raspberry Pi OS Bookwormにインストール済みのBlueZ 5.66では、バグによってSesame 5のGATT Serviceが取得できず正常に動作しません。BlueZ 5.68で修正済みですが、Raspberry Pi OS TrixieにアップグレードしてBlueZ 5.82を使用するのが最も簡単で確実です。

- Windows環境でも動作しますが、signalが使えないため、`Ctrl+C`で終了時に、MQTTやSesame 5の切断処理を行いません。特に問題はないですが、想定どおりの動作ではない旨をご留意ください。

- macOS環境でも動作すると思われますが、動作未確認です。

- このスクリプトは、[gomalock](https://github.com/meronepy/gomalock)ライブラリの動作確認を目的として作成されています。BLE通信は暗号化されていますが、MQTTとは平文で通信します。ユーザーとパスワード認証を使用することもできますが、それでもセキュリティリスクが生じます。また、動作保証もありません。自己責任でご使用ください。

---

## 🔌 準備

### 🧰 インストール手順 (Raspberry Pi OSの場合)

1. GithubのReleasesから、最新の`ssm2mqtt.zip`をダウンロード。

2. zipファイルを展開、`/usr/local/bin/ssm2mqtt`に配置。

    ```bash
    unzip ssm2mqtt.zip
    sudo cp -r ssm2mqtt /usr/local/bin/ssm2mqtt
    ```

3. ファイルの所有者を変更。

    ```bash
    sudo chown -R ${USER}: /usr/local/bin/ssm2mqtt
    ```

4. 仮想環境を構築。

    ```bash
    cd /usr/local/bin/ssm2mqtt
    python -m venv .venv
    ```

5. バックエンドのライブラリをインストール。

     ```bash
     .venv/bin/pip install -r requirements.txt
     ```

6. `config.json`を編集。

    ```bash
    nano config.json
    ```

以下は自動起動を設定する場合の追加の手順。

1. Serviceの作成と有効化。

    ```bash
    sudo cp ssm2mqtt.service /etc/systemd/system/ssm2mqtt.service
    sudo systemctl enable ssm2mqtt.service
    ```

    > `ssm2mqtt.service`はローカル環境でmosquittoを使用する場合の例です。環境に合わせて編集してください。

### ▶️ 実行方法 (Raspberry Pi OSの場合)

- 自動起動を設定済みの場合

    ```bash
    sudo systemctl start ssm2mqtt
    ```

- 自動起動を設定していない場合

    ```bash
    /usr/local/bin/ssm2mqtt/.venv/bin/python /usr/local/bin/ssm2mqtt/main.py
    ```

---

## 🔑 操作方法 (mosquittoを使用する場合)

### 📤 Sesame 5の操作

`config.json`で`topic_subscribe`に設定したトピックに、`lock_command`、`unlock_command`または`toggle_command`で設定した文字をパブリッシュすることで操作できます。

施錠の操作例

```bash
mosquitto_pub -t "ssm2mqtt/setLockTargetState" -m "S"
```

開錠の操作例

```bash
mosquitto_pub -t "ssm2mqtt/setLockTargetState" -m "U"
```

トグル(施錠中の場合は開錠、開錠中の場合は施錠)の操作例

```bash
mosquitto_pub -t "ssm2mqtt/setLockTargetState" -m "T"
```

### 📥 Sesame 5の状態の受信

`config.json`で`topic_publish`に設定したトピックをサブスクライブすると取得できます。Sesame 5の状態が変化するとリアルタイムでパブリッシュされます。手動で状態を取得することはできません。

受信例

```bash
mosquitto_sub -t "ssm2mqtt/getLockCurrentState"
```

デバイスの状態は以下のフォーマットでパブリッシュされます。

```json
{"lockCurrentState": "S", "batteryLevel": 100, "chargingState": "NOT_CHARGEABLE", "statusLowBattery": false}
```

`lockCurrentState`は、`config.json`で`lock_command`または`unlock_command`に設定した文字が表示されます。

`chargingState`は、定数であり`NOT_CHARGEABLE`から変化しません。homebridge-mqttthingなどで使用するための項目です。

---

## ⚙️ config.jsonについて

`ssm2mqtt`の設定ファイルです。`main.py`と同じディレクトリに配置する必要があります。

### 🍀 設定例

```json
{
    "mqtt": {
        "host": "localhost",
        "port": 1883,
        "topic_publish": "ssm2mqtt/getLockCurrentState",
        "topic_subscribe": "ssm2mqtt/setLockTargetState",
        "user": "",
        "password": ""
    },
    "sesame": {
        "mac_address": "XX:XX:XX:XX:XX:XX",
        "secret_key": "1234567890abcdef1234567890abcdef",
        "history_name": "ssm2mqtt",
        "lock_command": "S",
        "unlock_command": "U",
        "toggle_command": "T"
    }
}
```

### 👀 キーの説明

|キー|説明|
|---|---|
|host|MQTTブローカーのIPアドレス。|
|port|MQTTブローカーのポート。|
|topic_publish|Sesame 5の状態をパブリッシュするトピック。|
|topic_subscribe|Sesame 5の操作をするためのトピック。|
|user|MQTTのユーザー名。空欄で無効。|
|password|MQTTのパスワード。空欄で無効。|
|mac_address|Sesame 5のMACアドレス。|
|secret_key|Sesame 5のシークレットキー。|
|history_name|操作履歴に表示する名前。|
|lock_command|施錠操作と、パブリッシュ時に施錠状態を表すために使用する文字。|
|unlock_command|開錠操作と、パブリッシュ時に開錠状態を表すために使用する文字。|
|toggle_command|トグル操作をするために使用する文字。|

### 🔍 設定値の取得方法

- `mac_address`は前述のインストールを済ませたうえで、同梱の`discover.py`を使用することで、周囲のSesameデバイスをスキャンして取得することができます。

    ```shell-session
    $ /usr/local/bin/ssm2mqtt/.venv/bin/python /usr/local/bin/ssm2mqtt/discover.py
    Address    : XX:XX:XX:XX:XX:XX
    Model      : Sesame 5
    Name       : None
    RSSI       : -88
    Registered : True
    UUID       : 12345678-90ab-cdef-1234-567890abcdef
    ```

- `secret_key`はmochipon様作成の[QR Code Reader for SESAME](https://sesame-qr-reader.vercel.app/)を使用して、マネージャー権限以上のQRコードから抽出できます。

---

## 🧩 homebridge-mqttthingでの設定例

```json
{
    "type": "lockMechanism",
    "name": "Sesame 5",
    "topics": {
        "getLockCurrentState": "ssm2mqtt/getLockCurrentState$.lockCurrentState",
        "getLockTargetState": "ssm2mqtt/getLockCurrentState$.lockCurrentState",
        "setLockTargetState": "ssm2mqtt/setLockTargetState",
        "getBatteryLevel": "ssm2mqtt/getLockCurrentState$.batteryLevel",
        "getChargingState": "ssm2mqtt/getLockCurrentState$.chargingState",
        "getStatusLowBattery": "ssm2mqtt/getLockCurrentState$.statusLowBattery"
    },
    "manufacturer": "CANDY HOUSE",
    "model": "Sesame 5",
    "serialNumber": "12345678-90ab-cdef-1234-567890abcdef",
    "firmwareRevision": "3.0",
    "accessory": "mqttthing"
}
```

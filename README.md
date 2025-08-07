# [ssm2mqtt](https://github.com/meronepy/ssm2mqtt)

![Python](https://img.shields.io/badge/python-3.13-5da1d8)
[![License](https://img.shields.io/badge/license-MIT-5da1d8)](LICENSE)
![Platform](https://img.shields.io/badge/platform-Linux%20%2F%20Windows%20%2F%20macOS-ffb8d2)

Bluetooth Low EnergyでSesame 5デバイスとMQTTをブリッジするPythonスクリプト。

HomebridgeやHome Assistantから、BLE経由でのSesame 5の操作を可能にします。

---

## 機能

- MQTTのメッセージに応じてSesame 5をBLE経由で施錠と開錠。
- サムターン位置、施錠状態、電池電圧、電池残量、電池不足警告をBLE経由でリアルタイムでMQTTにパブリッシュ。

---

## 使用方法

### セサミの操作

`ベーストピック/セサミのUUID/set`トピックに、`LOCK`または`UNLOCK`をパブリッシュすることで操作できます。

セサミのUUIDは公式アプリまたは[後述の方法](#設定値の取得方法)で確認できます。

施錠例

```bash
mosquitto_pub -t "ssm2mqtt/12345678-90ab-cdef-1234-567890abcdef/set" -m "LOCK"
```

開錠例

```bash
mosquitto_pub -t "ssm2mqtt/12345678-90ab-cdef-1234-567890abcdef/set" -m "UNLOCK"
```

### セサミの状態の受信

`ベーストピック/セサミのUUID/get`トピックをサブスクライブすると取得できます。

セサミの状態が変化するとリアルタイムでパブリッシュされます。手動で状態を取得することはできません。

受信例

```bash
mosquitto_sub -t "ssm2mqtt/12345678-90ab-cdef-1234-567890abcdef/get"
```

デバイスの状態は以下のフォーマットでパブリッシュされます。

```json
{"position": -13, "lockCurrentState": "LOCKED", "batteryVoltage": 6.062, "batteryLevel": 100, "chargingState": "NOT_CHARGEABLE", "statusLowBattery": false}
```

`chargingState`は、定数であり`NOT_CHARGEABLE`から変化しません。homebridge-mqttthingなどで使用するための項目です。

---

## 準備

### インストール手順 (Raspberry Pi OSの場合)

1. GithubのReleasesから、最新の`ssm2mqtt.zip`をダウンロード。

2. zipファイルを展開、`/usr/local/bin/ssm2mqtt`に配置。

    ```bash
    unzip "ssm2mqtt*.zip"
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

以降は自動起動を設定する場合の追加の手順。

1. Serviceの作成と有効化。

    ```bash
    sudo cp ssm2mqtt.service /etc/systemd/system/ssm2mqtt.service
    sudo systemctl enable ssm2mqtt.service
    ```

### 実行方法 (Raspberry Pi OSの場合)

- 自動起動を設定済みの場合

    ```bash
    sudo systemctl start ssm2mqtt
    ```

- 自動起動を設定していない場合

    ```bash
    /usr/local/bin/ssm2mqtt/.venv/bin/python /usr/local/bin/ssm2mqtt/main.py
    ```

---

## config.jsonについて

`ssm2mqtt`の設定ファイルです。`main.py`と同じディレクトリに配置する必要があります。

### 設定値の取得方法

- MACアドレスは前述のインストールを済ませたうえで、同梱の`discover.py`を使用することで、周囲のセサミをスキャンできます。
- セサミ公式アプリのUUIDと比較して、目的のセサミのMACアドレスを取得してください。

    ```shell-session
    $ /usr/local/bin/ssm2mqtt/.venv/bin/python /usr/local/bin/ssm2mqtt/discover.py
    Address    : XX:XX:XX:XX:XX:XX
    Model      : Sesame 5
    Name       : None
    RSSI       : -88
    Registered : True
    UUID       : 12345678-90ab-cdef-1234-567890abcdef
    ```

- シークレットキーはmochipon様作成の[QR Code Reader for SESAME](https://sesame-qr-reader.vercel.app/)を使用して、マネージャー権限以上のQRコードから抽出できます。

### 設定例

```json
{
    "history_name": "ssm2mqtt",
    "mqtt": {
        "base_topic": "ssm2mqtt",
        "host": "localhost",
        "port": 1883,
        "user": "",
        "password": ""
    },
    "devices": {
        "XX:XX:XX:XX:XX:XX": "1234567890abcdef1234567890abcdef",
        "YY:YY:YY:YY:YY:YY": "1234567890abcdef1234567890abcdef",
        "ZZ:ZZ:ZZ:ZZ:ZZ:ZZ": "1234567890abcdef1234567890abcdef"
    }
}
```

### キーの説明

|キー|説明|
|---|---|
|history_name|操作履歴に表示する名前。|
|base_topic|ssm2mqttが使用する共通のルートトピック|
|host|MQTTブローカーのIPアドレス。|
|port|MQTTブローカーのポート。|
|user|MQTTのユーザー名。空欄で無効。|
|password|MQTTのパスワード。空欄で無効。|
|devices|セサミのMACアドレスとシークレットキーのペア。|

> `devices`には複数のセサミを設定できます。

---

## 対応機種

|対応状況|機種|
|:-:|:-:|
|✅|Sesame 5|
|⚠️|Sesame 5 Pro|
|⚠️|Sesame 5 USA|
|❌|Sesame 4以前|

Sesame 5 ProとSesame 5 USAは恐らく動作しますが、動作未確認です。

Sesame 4以前はOSが違うため動作しません。対応予定もありません。

---

## 開発環境

- Windows 11 24H2, Python 3.13.3
- Raspberry Pi Zero 2W, Raspberry Pi OS Trixie (64bit), Python 3.13.3
- Sesame 5, 3.0-5-18a8e4

---

## 注意事項

- Linuxで動作させる場合、 **BlueZ 5.82以降を強く推奨します。** Raspberry Pi OS Bookwormにインストール済みのBlueZ 5.66では、バグによってSesame 5のGATT Serviceが取得できず正常に動作しません。BlueZ 5.68で修正済みですが、Raspberry Pi OS TrixieにアップグレードしてBlueZ 5.82を使用するのが最も簡単で確実です。

- Python 3.13以降が必要です。

- macOS環境でも動作すると思われますが、動作未確認です。

- 複数のセサミと接続できる設計ですが、1台しか持っていないため実機での動作は未確認です。

- このスクリプトは、[gomalock](https://github.com/meronepy/gomalock)ライブラリの動作確認を目的として作成されています。BLE通信は暗号化されていますが、MQTTとは平文で通信します。ユーザーとパスワード認証を使用することもできますが、それでもセキュリティリスクが生じます。また、動作保証もありません。自己責任でご使用ください。

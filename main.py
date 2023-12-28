# --------------------------------------------------------------
# Bluetooth 相关的 UUID, 等值, 参考 https://www.bluetooth.com 中的
# specifications -> assigned-numbers > Assigned Numbers Repository (YAML)
#
# UUID 定义:
#      https://bitbucket.org/bluetooth-SIG/public/src/main/assigned_numbers/uuids/
#
# characteristic 值含义:
#      https://bitbucket.org/bluetooth-SIG/public/src/main/gss/
# --------------------------------------------------------------

import asyncio
import random
import struct
import sys
import os
import logging
import time

from bumble.core import AdvertisingData, UUID
from bumble.device import Device, Connection, DeviceConfiguration
from bumble.hci import Address
from bumble.host import Host
from bumble.profiles.battery_service import BatteryService
from bumble.profiles.device_information_service import DeviceInformationService
from bumble.transport import open_transport_or_link
from bumble.att import ATT_Error, ATT_INSUFFICIENT_ENCRYPTION_ERROR
from bumble.gatt import (
    Service,
    Characteristic,
    CharacteristicValue,
    Descriptor,
    GATT_CHARACTERISTIC_USER_DESCRIPTION_DESCRIPTOR,
    GATT_MANUFACTURER_NAME_STRING_CHARACTERISTIC,
    GATT_DEVICE_INFORMATION_SERVICE, GATT_MODEL_NUMBER_STRING_CHARACTERISTIC, GATT_SERIAL_NUMBER_STRING_CHARACTERISTIC,
    GATT_SOFTWARE_REVISION_STRING_CHARACTERISTIC, GATT_HARDWARE_REVISION_STRING_CHARACTERISTIC,
    GATT_FIRMWARE_REVISION_STRING_CHARACTERISTIC, GATT_GENERIC_ACCESS_SERVICE, GATT_DEVICE_NAME_CHARACTERISTIC,
    GATT_APPEARANCE_CHARACTERISTIC, GATT_HEART_RATE_SERVICE, GATT_HEART_RATE_MEASUREMENT_CHARACTERISTIC,
    GATT_BODY_SENSOR_LOCATION_CHARACTERISTIC, DelegatedCharacteristicAdapter, GATT_CYCLING_POWER_SERVICE,
    GATT_CYCLING_SPEED_AND_CADENCE_SERVICE,
)


class CyclingSensorsSimulator:
    def __init__(self):
        self.power = 260
        self.cadence = 72
        self.heart_rate = 100
        self.accumulated_torque = 0
        self.accumulated_rpm = 0
        self.last_rpm_update_ts = time.time()
        self.battery_level = random.randint(50, 99)  # 每次启动后固定

    # https://bitbucket.org/bluetooth-SIG/public/src/main/gss/org.bluetooth.characteristic.heart_rate_measurement.yaml
    def read_heart_rate(self, connection) -> bytes:
        flags = 0b00
        self.heart_rate = random.randint(155, 165)
        data = bytes([flags]) + struct.pack('B', self.heart_rate)
        return data

    # https://bitbucket.org/bluetooth-SIG/public/src/main/gss/org.bluetooth.characteristic.cycling_power_measurement.yaml
    def read_cycling_power(self, connection) -> bytes:
        now = time.time()

        self.power = random.randint(250, 300)
        data = struct.pack('<H', int(self.power))

        # 有左右平衡
        flags = 0b01
        flags |= 0b10   # 左参考 [LeftPower/(LeftPower + RightPower)]*100    ??? 不起作用
        balance = random.randint(45, 55) * 2
        data += struct.pack('b', balance)

        # 有扭矩
        flags |= 0b100
        flags |= 0b1000  # 累积扭矩源 0 = 基于轮子 1 = 基于曲柄
        self.accumulated_torque += 0.1  # random.randint(1, 1)
        self.accumulated_torque %= 0xffff
        data += struct.pack('<H', int(self.accumulated_torque) & 0xffff)

        target_rpm = random.randint(80, 95)  # 目标踏频
        self.accumulated_rpm += (target_rpm * (now - self.last_rpm_update_ts) / 60)
        self.accumulated_rpm %= 0xffff
        flags |= 0b100000
        data += struct.pack('<H', int(self.accumulated_rpm) & 0xffff)
        data += struct.pack('<H', int(self.last_rpm_update_ts * 1024) & 0xffff)
        self.last_rpm_update_ts = now
        logging.info("read rpm: %d, %d", self.accumulated_rpm, self.last_rpm_update_ts)

        data = struct.pack('<H', flags) + data
        return data

    def read_battery_level(self, connection) -> int:
        logging.info("read battery level")
        return self.battery_level


async def main():
    async with await open_transport_or_link("usb:0") as (hci_source, hci_sink):
        config = DeviceConfiguration()
        config.name = "CyclingSensors"
        config.address = Address("F0:F1:F2:F3:F4:F6")
        config.advertising_interval_min = 1000
        config.advertising_interval_max = 2000
        config.keystore = "JsonKeyStore"
        config.irk = bytes.fromhex("865F81FF5A8B486EAAE29A27AD9F77DC")
        config.advertising_data = bytes(
            AdvertisingData(
                [
                    (AdvertisingData.COMPLETE_LOCAL_NAME, bytes(config.name, 'utf-8')),
                    (AdvertisingData.INCOMPLETE_LIST_OF_16_BIT_SERVICE_CLASS_UUIDS, bytes(GATT_CYCLING_POWER_SERVICE)),
                    (AdvertisingData.APPEARANCE, struct.pack('<H', 0x0340))
                ]
            )
        )
        host = Host(controller_source=hci_source, controller_sink=hci_sink)
        device = Device(config=config, host=host, generic_access_service=False)

        # GATT_DEVICE_INFORMATION_SERVICE
        device_info_service = Service(
            GATT_DEVICE_INFORMATION_SERVICE, [
                # 制造商名称
                Characteristic(
                    GATT_MANUFACTURER_NAME_STRING_CHARACTERISTIC,
                    Characteristic.Properties.READ,
                    Characteristic.READABLE,
                    'PangLiang Technology',
                )
                # 型号
                , Characteristic(
                    GATT_MODEL_NUMBER_STRING_CHARACTERISTIC,
                    Characteristic.Properties.READ,
                    Characteristic.READABLE,
                    '22',
                )
                # 序列号
                , Characteristic(
                    GATT_SERIAL_NUMBER_STRING_CHARACTERISTIC,
                    Characteristic.Properties.READ,
                    Characteristic.READABLE,
                    '214301961',
                )
                # 固件版本
                , Characteristic(
                    GATT_FIRMWARE_REVISION_STRING_CHARACTERISTIC,
                    Characteristic.Properties.READ,
                    Characteristic.READABLE,
                    '0.106',
                )
                # 硬件版本
                , Characteristic(
                    GATT_HARDWARE_REVISION_STRING_CHARACTERISTIC,
                    Characteristic.Properties.READ,
                    Characteristic.READABLE,
                    '1.18',
                )
                # 软件版本
                , Characteristic(
                    GATT_SOFTWARE_REVISION_STRING_CHARACTERISTIC,
                    Characteristic.Properties.READ,
                    Characteristic.READABLE,
                    '17.06',
                )
            ]
        )

        # GATT_GENERIC_ACCESS_SERVICE
        generic_access_service = Service(
            GATT_GENERIC_ACCESS_SERVICE, [
                # 设备名称
                Characteristic(
                    GATT_DEVICE_NAME_CHARACTERISTIC,
                    Characteristic.Properties.READ,
                    Characteristic.READABLE,
                    'CyclingSensors',
                ),
                # 设备式样
                # https://bitbucket.org/bluetooth-SIG/public/src/main/assigned_numbers/core/appearance_values.yaml
                Characteristic(
                    GATT_APPEARANCE_CHARACTERISTIC,
                    Characteristic.Properties.READ,
                    Characteristic.READABLE,
                    bytes([0x003, 0x02])  # 0x003: Watch, 0x02: Smartwatch
                ),
            ]
        )

        simulator = CyclingSensorsSimulator()

        # GATT_HEART_RATE_SERVICE
        heart_rate_service = Service(
            GATT_HEART_RATE_SERVICE, [
                # 心率测量
                Characteristic(
                    GATT_HEART_RATE_MEASUREMENT_CHARACTERISTIC,
                    Characteristic.Properties.NOTIFY,
                    Characteristic.READABLE,
                    CharacteristicValue(read=simulator.read_heart_rate),
                ),
                # 心率设备位置
                # https://bitbucket.org/bluetooth-SIG/public/src/main/gss/org.bluetooth.characteristic.body_sensor_location.yaml
                Characteristic(
                    GATT_BODY_SENSOR_LOCATION_CHARACTERISTIC,
                    Characteristic.Properties.READ,
                    Characteristic.READABLE,
                    bytes([0x02]),
                ),
            ]
        )

        # GATT_CYCLING_POWER_SERVICE
        cycling_power_service = Service(
            GATT_CYCLING_POWER_SERVICE, [
                # 功率
                Characteristic(
                    UUID.from_16_bits(0x2A63, 'Cycling Power Measurement'),
                    Characteristic.Properties.NOTIFY,
                    Characteristic.READABLE,
                    CharacteristicValue(read=simulator.read_cycling_power),
                ),
                # 功能描述
                # https://bitbucket.org/bluetooth-SIG/public/src/main/gss/org.bluetooth.characteristic.cycling_power_feature.yaml
                Characteristic(
                    UUID.from_16_bits(0x2A65, 'Cycling Power Feature'),
                    Characteristic.Properties.READ,
                    Characteristic.READABLE,
                    bytes([0b1011, 00, 80, 00]),  # bit0: 支持踏板功率平衡, bit1: 支持扭矩, bit3: 支持踏频
                ),
                # 设备位置
                # https://bitbucket.org/bluetooth-SIG/public/src/main/gss/org.bluetooth.characteristic.sensor_location.yaml
                Characteristic(
                    UUID.from_16_bits(0x2A5D, 'Sensor Location'),
                    Characteristic.Properties.READ,
                    Characteristic.READABLE,
                    bytes([0x00]),
                ),
            ]
        )

        battery_service = BatteryService(simulator.read_battery_level)

        device.add_services([
            device_info_service,
            generic_access_service,
            heart_rate_service,
            cycling_power_service,
            battery_service
        ])

        logging.info("Starting device")

        # Get things going
        await device.power_on()
        await device.start_advertising(auto_restart=True)
        while True:
            await asyncio.sleep(1)
            await device.notify_subscribers(heart_rate_service.characteristics[0])
            await device.notify_subscribers(cycling_power_service.characteristics[0])


# -----------------------------------------------------------------------------
logging.basicConfig(level=os.environ.get('BUMBLE_LOGLEVEL', 'INFO').upper())
asyncio.run(main())

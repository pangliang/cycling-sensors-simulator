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
import pathlib
import random
import struct
import sys
import os
import logging
import time
from typing import Optional
import winreg

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
from windows_toasts import AudioSource, Toast, ToastAudio, ToastDisplayImage, WindowsToaster

from register_hkey_aumid import register_hkey


class CyclingSensorsSimulator:
    def __init__(self):
        self.running = True
        self.choise_weights = [1, 3, 5, 3, 1]
        self.target_index_fn = lambda: random.choices([0, 1, 2, 3, 4], weights=self.choise_weights )[0]
        self.target_index = self.target_index_fn()
        self.power_target_choices = [140, 160, 200, 220, 240]
        self.heart_rate_target_choices = [135, 145, 165, 175, 180]
        self.cadence_target_choices = [70, 75, 80, 85, 90]

        self.power_target = self.power_target_choices[0]
        self.power_change_fn = lambda: random.randint(1, 5)
        self.power = self.power_target

        self.heart_rate_target = self.heart_rate_target_choices[0]
        self.heart_rate = self.heart_rate_target
        self.heart_rate_change_fn = lambda: random.randint(1, 2)
        self.heart_rate_step_length = 5

        self.accumulated_torque = 0
        self.accumulated_rpm = 0
        self.last_rpm_update_ts = time.time()

        self.cadence_target = self.cadence_target_choices[0]
        self.cadence = self.cadence_target
        self.cadence_change_fn = lambda: random.randint(1, 2)

        self.step_length = 0
        self.step_length_fn = lambda: random.randint(3, 15)
        self.battery_level = random.randint(50, 99)  # 每次启动后固定

    def loop(self):
        # 检查步长是否结束
        if self.step_length > 0:
            self.step_length -= 1
        else:
            # 确定步长
            self.step_length = self.step_length_fn()
            # 确定目标索引
            self.target_index = self.target_index_fn()
            # 功率目标
            self.power_target = self.power_target_choices[self.target_index]
            # 踏频目标
            self.cadence_target = self.cadence_target_choices[self.target_index]
            logging.info("功率步长:%d, 功率目标:%d, 踏频目标:%d", self.step_length, self.power_target, self.cadence_target)

        # 检查心率步长是否结束
        if self.heart_rate_step_length > 0:
            self.heart_rate_step_length -= 1
        else:
            # 确定心率步长
            self.heart_rate_step_length = self.step_length + random.randint(5, 10)
            # 心率目标
            self.heart_rate_target = self.heart_rate_target_choices[self.target_index]
            logging.info("心率步长:%d, 心率目标:%d", self.heart_rate_step_length, self.heart_rate_target)

        # 功率
        if self.power < self.power_target:
            self.power += self.power_change_fn()
        elif self.power > self.power_target:
            self.power -= self.power_change_fn()

        # 心率
        if self.heart_rate < self.heart_rate_target:
            self.heart_rate += self.heart_rate_change_fn()
        elif self.heart_rate > self.heart_rate_target:
            self.heart_rate -= self.heart_rate_change_fn()

        # cadence
        if self.cadence < self.cadence_target:
            self.cadence += self.cadence_change_fn()
        elif self.cadence > self.cadence_target:
            self.cadence -= self.cadence_change_fn()

    # https://bitbucket.org/bluetooth-SIG/public/src/main/gss/org.bluetooth.characteristic.heart_rate_measurement.yaml
    def read_heart_rate(self, connection) -> bytes:
        flags = 0b00
        data = bytes([flags]) + struct.pack('B', int(self.heart_rate) + random.randint(-2, 2))
        return data

    # https://bitbucket.org/bluetooth-SIG/public/src/main/gss/org.bluetooth.characteristic.cycling_power_measurement.yaml
    def read_cycling_power(self, connection) -> bytes:
        now = time.time()
        data = struct.pack('<h', int(self.power + random.randint(-5, 5)))

        # 有左右平衡
        flags = 0b01
        flags |= 0b10   # 左参考 [LeftPower/(LeftPower + RightPower)]*100    ??? 不起作用
        balance = random.randint(45, 55) * 2
        data += struct.pack('b', balance)

        # # 有扭矩
        flags |= 0b100
        flags |= 0b1000  # 累积扭矩源 0 = 基于轮子 1 = 基于曲柄
        self.accumulated_torque += random.randint(1, 1) * 34
        self.accumulated_torque %= 0xffff
        data += struct.pack('<H', int(self.accumulated_torque) & 0xffff)

        # while now - self.last_rpm_update_ts < 60 / self.cadence:
        #     time.sleep(0.1)
        #     now = time.time()
        # self.accumulated_rpm += 1  # self.cadence * (now - self.last_rpm_update_ts) / 60
        # self.accumulated_rpm %= 0xffff
        # flags |= 0b100000
        # data += struct.pack('<H', int(self.accumulated_rpm) & 0xffff)
        # data += struct.pack('<H', int(self.last_rpm_update_ts * 1024) & 0xffff)
        # self.last_rpm_update_ts = now

        data = struct.pack('<H', flags) + data
        return data

    # https://bitbucket.org/bluetooth-SIG/public/src/main/gss/org.bluetooth.characteristic.csc_measurement.yaml
    def read_cadence(self, connection) -> bytes:
        now = time.time()
        while now - self.last_rpm_update_ts < 60 / self.cadence:
            time.sleep(0.05)
            now = time.time()

        self.accumulated_rpm += 1
        self.accumulated_rpm %= 0xffff
        self.last_rpm_update_ts = now
        data = bytes([0b10])
        data += struct.pack('<H', int(self.accumulated_rpm) & 0xffff)
        data += struct.pack('<H', int(self.last_rpm_update_ts * 1024) & 0xffff)
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
                    (AdvertisingData.INCOMPLETE_LIST_OF_16_BIT_SERVICE_CLASS_UUIDS, bytes(GATT_CYCLING_SPEED_AND_CADENCE_SERVICE)),
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
        # GATT_CYCLING_SPEED_AND_CADENCE_SERVICE
        cycling_speed_and_cadence_service = Service(
            GATT_CYCLING_SPEED_AND_CADENCE_SERVICE, [
                # 踏频
                Characteristic(
                    UUID.from_16_bits(0x2A5B, 'CSC Measurement'),
                    Characteristic.Properties.NOTIFY,
                    Characteristic.READABLE,
                    CharacteristicValue(read=simulator.read_cadence),
                ),
                # 功能描述
                # https://bitbucket.org/bluetooth-SIG/public/src/main/gss/org.bluetooth.characteristic.csc_feature.yaml
                Characteristic(
                    UUID.from_16_bits(0x2A5C, 'CSC Feature'),
                    Characteristic.Properties.READ,
                    Characteristic.READABLE,
                    bytes([0x02, 0x00]),  # bit1: Crank Revolution Data Supported
                ),
                # 设备位置
                # https://bitbucket.org/bluetooth-SIG/public/src/main/gss/org.bluetooth.characteristic.sensor_location.yaml
                Characteristic(
                    UUID.from_16_bits(0x2A5D, 'Sensor Location'),
                    Characteristic.Properties.READ,
                    Characteristic.READABLE,
                    bytes([0x05]),
                ),
            ]
        )

        battery_service = BatteryService(simulator.read_battery_level)

        device.add_services([
            device_info_service,
            generic_access_service,
            heart_rate_service,
            cycling_power_service,
            cycling_speed_and_cadence_service,
            battery_service
        ])

        logging.info("Starting device")

        # Get things going
        await device.power_on()
        await device.start_advertising(auto_restart=True)

        toaster = WindowsToaster('cycling-sensors-simulator')
        newToast = Toast()
        newToast.text_fields = ['程序启动!']
        toaster.show_toast(newToast)

        start_time = time.time()
        duration = 65 * 60
        async def main_loop():
            while simulator.running:
                # 如果从开始到现在的时间大于5分钟, 则发送window通知, 并退出程序
                if time.time() - start_time > duration:
                    simulator.running = False
                    return

                await asyncio.sleep(0.5)
                simulator.loop()
                await device.notify_subscribers(heart_rate_service.characteristics[0])
                await device.notify_subscribers(cycling_power_service.characteristics[0])

        async def cadence_loop():
            while simulator.running:
                await asyncio.sleep(0.3)
                await device.notify_subscribers(cycling_speed_and_cadence_service.characteristics[0])

        await asyncio.gather(main_loop(), cadence_loop())

        await device.stop_advertising()
        await device.power_off()

        toaster = WindowsToaster(app_name)
        newToast = Toast()
        newToast.text_fields = ['程序即将退出!']
        newToast.AddImage(ToastDisplayImage.fromPath(os.path.abspath(os.path.join(os.path.dirname(__file__), "icon.ico"))))
        newToast.audio = ToastAudio(AudioSource.IM, looping=True)
        toaster.show_toast(newToast)

# -----------------------------------------------------------------------------
        
app_name = "cycling-sensors-simulator"

toaster = WindowsToaster(app_name)
newToast = Toast()
newToast.text_fields = ['程序启动!']
newToast.AddImage(ToastDisplayImage.fromPath(os.path.abspath(os.path.join(os.path.dirname(__file__), "icon.ico"))))
toaster.show_toast(newToast)

logging.basicConfig(level=os.environ.get('BUMBLE_LOGLEVEL', 'INFO').upper())
asyncio.run(main())

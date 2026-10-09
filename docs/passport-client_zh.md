# AI Passport 自建服务客户端

基于 [FoloToy/folo-ai-passport-xiaozhi](https://github.com/FoloToy/folo-ai-passport-xiaozhi)，
保留小智 2.5.0 核心和原 MIT 许可。适用于 FoloToy AI Passport：ESP32-C3、8 MB Flash、
ES8311 音频、ST7789 屏幕及 CW2017 电量计。

## 本版本改动

- 默认使用自建 OTA 地址，读取返回的 `websocket.url`、`token` 和 `version`。
- 按键打断时立即清空解码队列和播放队列，并忽略后续音频，直到下一次 TTS start。
- 使用原子变量在主任务与网络回调之间传递打断状态。
- 音量 0–80% 沿用原曲线；90% 比原来的同档增加 3 dB；100% 比原来的最大值增加 6 dB。
- ES8311 音量寄存器读回日志用于核对增益，麦克风增益保持原值。

音量增益是配置值，不代表实测声压增幅。先试听 80–90%，出现破音时降低音量。
已交给音频硬件的样本可能仍产生短暂尾音，清空队列不能撤回这些样本。

## 服务端配置

默认 OTA 地址：

```text
https://sealtun-xiaozhi-doubao-ota-ns-d5n27ymc.sealosgzg.site/xiaozhi/ota/
```

OTA 通过 POST 请求下发语音地址及认证令牌。目前语音地址为：

```text
wss://sealtun-xiaozhi-doubao-voice-ns-d5n27ymc.sealosgzg.site/xiaozhi/v1/
```

认证令牌由 OTA 在运行时提供，未写入源码。此服务依赖运行网关的电脑联网，
以及网关和两条 Sealtun 隧道持续运行；隧道地址可能随部署变化。

使用自己的服务时，在启动阶段按 OK 进入配网，连接 `Xiaozhi-xxxx` 热点，
打开 `http://192.168.4.1`，在高级设置保存自己的 OTA 地址并重启。
NVS 已保存的 OTA 地址优先于固件默认值。OTA 地址使用 HTTP(S)，语音地址使用 WS(S)。

也可以修改 `main/boards/folotoy/ai-passport/config.json` 中的 `CONFIG_OTA_URL`，
然后重新编译。

## 编译

使用 ESP-IDF 6.1；最低支持 6.0.1，不支持 IDF 5.x。

```sh
source /path/to/esp-idf/export.sh
python3 scripts/build.py folotoy/ai-passport --name ai-passport --language zh-CN
```

输出 `build/xiaozhi.bin` 应用固件、`build/merged-binary.bin` 完整镜像及 `build/flash_args`。

## 烧录

### 已使用本项目分区布局的设备

先确认设备使用 `partitions/v2/8m.csv`，以及当前启动的是 `ota_0`（地址 `0x20000`）。
随后可仅更新应用固件，保留 Wi-Fi、UUID 等 NVS 设置：

```sh
python -m esptool --chip esp32c3 --port YOUR_PORT --baud 460800 \
  --before usb-reset --after hard-reset write-flash 0x20000 xiaozhi.bin
```

上述命令使用 esptool 5.x 语法。若当前启动槽位为 `ota_1`，应使用对应槽位地址或 OTA 更新，
不能直接套用 `ota_0` 的地址。

### 原厂固件或其他分区布局

原厂 Passport 与本项目的分区布局不同，仅写 `xiaozhi.bin` 不足以完成迁移。
先备份完整 8 MB Flash，再按完整镜像或生成的 `flash_args` 写入。

完整镜像 `merged-binary.bin` 写入地址是 `0x0`；会覆盖旧分区、固件和设置，
烧录后可能需要重新配网。它不是保留原厂数据的升级包。
原设备的完整备份、NVS 数据及私有串口日志未包含在发布附件中。

## 按键

- OK：待机时开始聊天，播放回答时打断；启动阶段进入配网。
- UP：音量 +10；长按到 100%。
- DOWN：音量 -10；长按静音。

## 验证记录（2026-10-09）

- ESP-IDF 6.1 / ESP32-C3 编译成功。
- 真机烧录通过写入校验，Wi-Fi 和设备 UUID 保留。
- OTA HTTPS 连接及语音 WSS 连接正常，已观察到语音识别和回答播放状态。
- 90% 音量寄存器为 `0xC2`，100% 为 `0xD2`，与新曲线相符。
- 用户试听反馈效果良好；未做声压仪测量、扬声器功率测试或完整续航测试。
- 主机测试执行 81 项；原仓库与修改版均有相同的 17 个失败、18 个错误，
  涉及此单板分支未包含的上游多板卡测试用例，没有新增失败项。
- 按键打断逻辑已加入；本次保存的串口验证记录未捕获打断事件，未单独测量停声延迟。

当前固件仍采用常亮屏幕与联网待机，未新增自动灭屏或深度休眠。

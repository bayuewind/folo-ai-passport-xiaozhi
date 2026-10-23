#include "wifi_board.h"
#include "display/lcd_display.h"
#include "codecs/es8311_audio_codec.h"
#include "application.h"
#include "button.h"
#include "config.h"
#include "assets/lang_config.h"
#include "cw2017_battery_monitor.h"

#include <esp_log.h>
#include <esp_sleep.h>
#include <esp_timer.h>
#include <driver/gpio.h>
#include <driver/ledc.h>
#include <esp_lcd_panel_vendor.h>
#include <button_adc.h>
#include <esp_adc/adc_oneshot.h>
#include <driver/i2c_master.h>
#include <driver/spi_common.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

#include <atomic>

#define TAG "AiPassport"

// Physical keys share one ADC pin through a resistor ladder (see config.h).
enum {
    kAdcButtonUp = 0,
    kAdcButtonDown,
    kAdcButtonOk,
    kAdcButtonNum,
};

class AiPassportBoard : public WifiBoard {
private:
    i2c_master_bus_handle_t codec_i2c_bus_;
    Button* adc_button_[kAdcButtonNum];
    adc_oneshot_unit_handle_t adc_handle_ = nullptr;
    LcdDisplay* display_;
    Cw2017BatteryMonitor* battery_;
    esp_lcd_panel_handle_t panel_ = nullptr;
    // True when a key press woke the chip from deep sleep. That press is still
    // being released while the firmware boots, so it must not count as a click.
    bool woke_from_deep_sleep_ = false;

#if CONFIG_SCREEN_OFF_UNLESS_VOLUME_KEY
    static constexpr int64_t kVolumeWakeUs = 10 * 1000 * 1000;
    static constexpr int64_t kScreenCheckUs = 250 * 1000;

    esp_timer_handle_t screen_timer_ = nullptr;
    std::atomic<int64_t> screen_wake_until_us_{0};
    std::atomic<bool> screen_on_{true};

    // Screens the user has to read, so the backlight stays on for them.
    static bool StateNeedsScreen(DeviceState state) {
        switch (state) {
            case kDeviceStateIdle:
            case kDeviceStateConnecting:
            case kDeviceStateListening:
            case kDeviceStateSpeaking:
            case kDeviceStateNotifying:
                return false;
            default:
                return true;
        }
    }

    bool ScreenWanted() {
        return StateNeedsScreen(Application::GetInstance().GetDeviceState()) ||
               esp_timer_get_time() < screen_wake_until_us_.load();
    }

    // Runs on the main task.
    void UpdateScreen() {
        bool want_on = ScreenWanted();
        if (want_on == screen_on_) {
            return;
        }
        screen_on_ = want_on;
        if (want_on) {
            GetBacklight()->RestoreBrightness();
        } else {
            GetBacklight()->SetBrightness(0);
        }
    }

    void WakeScreenForVolumeKey() {
        screen_wake_until_us_ = esp_timer_get_time() + kVolumeWakeUs;
        Application::GetInstance().Schedule([this]() { UpdateScreen(); });
    }

    void InitializeScreenTimer() {
        esp_timer_create_args_t args = {};
        args.callback = [](void* arg) {
            auto self = static_cast<AiPassportBoard*>(arg);
            if (self->ScreenWanted() != self->screen_on_) {
                Application::GetInstance().Schedule([self]() { self->UpdateScreen(); });
            }
        };
        args.arg = this;
        args.dispatch_method = ESP_TIMER_TASK;
        args.name = "screen_off";
        ESP_ERROR_CHECK(esp_timer_create(&args, &screen_timer_));
        ESP_ERROR_CHECK(esp_timer_start_periodic(screen_timer_, kScreenCheckUs));
    }
#endif

#if CONFIG_DEEP_SLEEP_IDLE_MINUTES > 0
    static constexpr int64_t kIdleSleepUs =
        static_cast<int64_t>(CONFIG_DEEP_SLEEP_IDLE_MINUTES) * 60 * 1000 * 1000;
    static constexpr int64_t kIdleCheckUs = 5 * 1000 * 1000;
    static constexpr gpio_num_t kKeyGpio = GPIO_NUM_0;

    esp_timer_handle_t idle_timer_ = nullptr;
    int64_t idle_since_us_ = 0;  // main task only, 0 = not idle
    std::atomic<bool> key_activity_{false};

    void CheckIdleSleep() {
        if (key_activity_.exchange(false) || !Application::GetInstance().CanEnterSleepMode()) {
            idle_since_us_ = 0;
            return;
        }
        int64_t now = esp_timer_get_time();
        if (idle_since_us_ == 0) {
            idle_since_us_ = now;
        } else if (now - idle_since_us_ >= kIdleSleepUs) {
            EnterDeepSleep();
        }
    }

    void EnterDeepSleep() {
        // A held key would wake the chip at once, so try again later.
        gpio_set_direction(kKeyGpio, GPIO_MODE_INPUT);
        gpio_set_pull_mode(kKeyGpio, GPIO_PULLUP_ONLY);
        if (gpio_get_level(kKeyGpio) == 0) {
            idle_since_us_ = 0;
            return;
        }
        ESP_LOGI(TAG, "Idle for %d min, entering deep sleep", CONFIG_DEEP_SLEEP_IDLE_MINUTES);

        auto codec = GetAudioCodec();
        codec->EnableInput(false);
        codec->EnableOutput(false);

        // Backlight pin low and held, panel in sleep mode.
        ledc_stop(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_0, DISPLAY_BACKLIGHT_OUTPUT_INVERT ? 1 : 0);
        gpio_set_direction(DISPLAY_BACKLIGHT_PIN, GPIO_MODE_OUTPUT);
        gpio_set_level(DISPLAY_BACKLIGHT_PIN, DISPLAY_BACKLIGHT_OUTPUT_INVERT ? 1 : 0);
        gpio_hold_en(DISPLAY_BACKLIGHT_PIN);
        gpio_deep_sleep_hold_en();
        if (panel_ != nullptr) {
            esp_lcd_panel_disp_sleep(panel_, true);
        }

        // All three keys pull GPIO0 below 1 V, which reads as low.
        ESP_ERROR_CHECK(esp_sleep_enable_gpio_wakeup_on_hp_periph_powerdown(BIT(kKeyGpio),
                                                                   ESP_GPIO_WAKEUP_GPIO_LOW));
        esp_deep_sleep_start();
    }

    void InitializeIdleTimer() {
        esp_timer_create_args_t args = {};
        args.callback = [](void* arg) {
            auto self = static_cast<AiPassportBoard*>(arg);
            Application::GetInstance().Schedule([self]() { self->CheckIdleSleep(); });
        };
        args.arg = this;
        args.dispatch_method = ESP_TIMER_TASK;
        args.name = "idle_sleep";
        ESP_ERROR_CHECK(esp_timer_create(&args, &idle_timer_));
        ESP_ERROR_CHECK(esp_timer_start_periodic(idle_timer_, kIdleCheckUs));
    }
#endif

    void NoteKeyActivity() {
#if CONFIG_DEEP_SLEEP_IDLE_MINUTES > 0
        key_activity_ = true;
#endif
    }

    void InitializeCodecI2c() {
        i2c_master_bus_config_t i2c_bus_cfg = {
            .i2c_port = I2C_NUM_0,
            .sda_io_num = AUDIO_CODEC_I2C_SDA_PIN,
            .scl_io_num = AUDIO_CODEC_I2C_SCL_PIN,
            .clk_source = I2C_CLK_SRC_DEFAULT,
            .glitch_ignore_cnt = 7,
            .intr_priority = 0,
            .trans_queue_depth = 0,
            .flags = {
                .enable_internal_pullup = 1,
            },
        };
        ESP_ERROR_CHECK(i2c_new_master_bus(&i2c_bus_cfg, &codec_i2c_bus_));

        // CW2017 fuel gauge is optional; a missing chip just disables battery UI.
        battery_ = new Cw2017BatteryMonitor(codec_i2c_bus_, BATTERY_CW2017_ADDR);
        battery_->Initialize();
    }

    void ChangeVolume(int delta) {
        auto codec = GetAudioCodec();
        auto volume = codec->output_volume() + delta;
        if (volume > 100) {
            volume = 100;
        }
        if (volume < 0) {
            volume = 0;
        }
        codec->SetOutputVolume(volume);
        GetDisplay()->ShowNotification(Lang::Strings::VOLUME + std::to_string(volume));
    }

    void ToggleChat() {
        auto& app = Application::GetInstance();
        if (app.GetDeviceState() == kDeviceStateStarting) {
            if (woke_from_deep_sleep_) {
                return;  // the key that woke the chip, not a request for Wi-Fi setup
            }
            EnterWifiConfigMode();
            return;
        }
        app.ToggleChatState();
    }

    void InitializeButtons() {
        for (int i = 0; i < kAdcButtonNum; i++) {
            adc_button_[i] = nullptr;
        }

        // One ADC1 unit shared by all three ladder keys. AdcButton reuses the
        // handle when adc_config.adc_handle is non-null, so the same physical
        // pin can decode several keys without "adc1 is already in use".
        adc_oneshot_unit_init_cfg_t init_cfg = {
            .unit_id = ADC_UNIT_1,
        };
        ESP_ERROR_CHECK(adc_oneshot_new_unit(&init_cfg, &adc_handle_));

        button_adc_config_t adc_cfg = {};
        adc_cfg.adc_handle = &adc_handle_;
        adc_cfg.unit_id = ADC_UNIT_1;
        adc_cfg.adc_channel = ADC_CHANNEL_0;  // GPIO0

        adc_cfg.button_index = kAdcButtonUp;      // UP:   ~0 mV
        adc_cfg.min = BSP_ADC_BUTTON_UP_MIN;
        adc_cfg.max = BSP_ADC_BUTTON_UP_MAX;
        adc_button_[kAdcButtonUp] = new AdcButton(adc_cfg);

        adc_cfg.button_index = kAdcButtonDown;    // DOWN: ~300 mV
        adc_cfg.min = BSP_ADC_BUTTON_DOWN_MIN;
        adc_cfg.max = BSP_ADC_BUTTON_DOWN_MAX;
        adc_button_[kAdcButtonDown] = new AdcButton(adc_cfg);

        adc_cfg.button_index = kAdcButtonOk;      // OK:   ~595 mV
        adc_cfg.min = BSP_ADC_BUTTON_OK_MIN;
        adc_cfg.max = BSP_ADC_BUTTON_OK_MAX;
        adc_button_[kAdcButtonOk] = new AdcButton(adc_cfg);

        // Button callbacks run on the button task; schedule all UI/audio
        // work onto the main task so LVGL and codec access stay on one thread.
        auto up = adc_button_[kAdcButtonUp];
        up->OnPressDown([this]() {
            NoteKeyActivity();
#if CONFIG_SCREEN_OFF_UNLESS_VOLUME_KEY
            WakeScreenForVolumeKey();
#endif
        });
        up->OnClick([this]() {
            Application::GetInstance().Schedule([this]() { ChangeVolume(10); });
        });
        up->OnLongPress([this]() {
            Application::GetInstance().Schedule([this]() {
                GetAudioCodec()->SetOutputVolume(100);
                GetDisplay()->ShowNotification(Lang::Strings::MAX_VOLUME);
            });
        });

        auto down = adc_button_[kAdcButtonDown];
        down->OnPressDown([this]() {
            NoteKeyActivity();
#if CONFIG_SCREEN_OFF_UNLESS_VOLUME_KEY
            WakeScreenForVolumeKey();
#endif
        });
        down->OnClick([this]() {
            Application::GetInstance().Schedule([this]() { ChangeVolume(-10); });
        });
        down->OnLongPress([this]() {
            Application::GetInstance().Schedule([this]() {
                GetAudioCodec()->SetOutputVolume(0);
                GetDisplay()->ShowNotification(Lang::Strings::MUTED);
            });
        });

        auto ok = adc_button_[kAdcButtonOk];
        ok->OnPressDown([this]() { NoteKeyActivity(); });
        ok->OnClick([this]() {
            Application::GetInstance().Schedule([this]() { ToggleChat(); });
        });
    }

    void InitializeSpi() {
        spi_bus_config_t buscfg = {};
        buscfg.mosi_io_num = DISPLAY_SPI_MOSI_PIN;
        buscfg.miso_io_num = GPIO_NUM_NC;
        buscfg.sclk_io_num = DISPLAY_SPI_SCK_PIN;
        buscfg.quadwp_io_num = GPIO_NUM_NC;
        buscfg.quadhd_io_num = GPIO_NUM_NC;
        buscfg.max_transfer_sz = DISPLAY_WIDTH * DISPLAY_HEIGHT * sizeof(uint16_t);
        ESP_ERROR_CHECK(spi_bus_initialize(SPI2_HOST, &buscfg, SPI_DMA_CH_AUTO));
    }

    void InitializeDisplay() {
        esp_lcd_panel_io_handle_t panel_io = nullptr;
        esp_lcd_panel_handle_t panel = nullptr;

        esp_lcd_panel_io_spi_config_t io_config = {};
        io_config.cs_gpio_num = DISPLAY_SPI_CS_PIN;
        io_config.dc_gpio_num = DISPLAY_DC_PIN;
        io_config.spi_mode = 0;
        io_config.pclk_hz = 40 * 1000 * 1000;
        io_config.trans_queue_depth = 10;
        io_config.lcd_cmd_bits = 8;
        io_config.lcd_param_bits = 8;
        ESP_ERROR_CHECK(esp_lcd_new_panel_io_spi(SPI2_HOST, &io_config, &panel_io));

        esp_lcd_panel_dev_config_t panel_config = {};
        panel_config.reset_gpio_num = DISPLAY_RST_PIN;  // -1 -> software reset
        panel_config.rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB;
        panel_config.bits_per_pixel = 16;
        ESP_ERROR_CHECK(esp_lcd_new_panel_st7789(panel_io, &panel_config, &panel));

        esp_lcd_panel_reset(panel);
        esp_lcd_panel_init(panel);

        // Panel-specific power/porch/gamma sequence for the ST7789P3 module
        // used on the Passport (copied from the original badge firmware).
        static const struct {
            uint8_t command;
            uint8_t data[16];
            uint8_t data_length;
            uint16_t delay_ms;
        } kSt7789P3InitCommands[] = {
            {0xB2, {0x05, 0x05, 0x00, 0x33, 0x33}, 5, 0},
            {0xB7, {0x35}, 1, 0},
            {0xBB, {0x21}, 1, 0},
            {0xC0, {0x2C}, 1, 0},
            {0xC2, {0x01}, 1, 0},
            {0xC3, {0x0B}, 1, 0},
            {0xC4, {0x20}, 1, 0},
            {0xC6, {0x0F}, 1, 0},
            {0xD0, {0xA7, 0xA1}, 2, 0},
            {0xD0, {0xA4, 0xA1}, 2, 0},
            {0xD6, {0xA1}, 1, 0},
            {0xE0, {0xD0, 0x04, 0x08, 0x0A, 0x09, 0x05, 0x2D, 0x43,
                    0x49, 0x09, 0x16, 0x15, 0x26, 0x2B}, 14, 0},
            {0xE1, {0xD0, 0x03, 0x09, 0x0A, 0x0A, 0x06, 0x2E, 0x44,
                    0x40, 0x3A, 0x15, 0x15, 0x26, 0x2A}, 14, 10},
        };
        for (const auto& cmd : kSt7789P3InitCommands) {
            esp_lcd_panel_io_tx_param(panel_io, cmd.command, cmd.data, cmd.data_length);
            if (cmd.delay_ms > 0) {
                vTaskDelay(pdMS_TO_TICKS(cmd.delay_ms));
            }
        }

        esp_lcd_panel_invert_color(panel, DISPLAY_INVERT_COLOR);
        esp_lcd_panel_set_gap(panel, DISPLAY_OFFSET_X, DISPLAY_OFFSET_Y);
        esp_lcd_panel_swap_xy(panel, DISPLAY_SWAP_XY);
        esp_lcd_panel_mirror(panel, DISPLAY_MIRROR_X, DISPLAY_MIRROR_Y);
        esp_lcd_panel_disp_on_off(panel, true);
        panel_ = panel;

        display_ = new SpiLcdDisplay(panel_io, panel,
                                     DISPLAY_WIDTH, DISPLAY_HEIGHT,
                                     DISPLAY_OFFSET_X, DISPLAY_OFFSET_Y,
                                     DISPLAY_MIRROR_X, DISPLAY_MIRROR_Y, DISPLAY_SWAP_XY);
    }

public:
    AiPassportBoard() : display_(nullptr), battery_(nullptr) {
        if (esp_sleep_get_wakeup_cause() == ESP_SLEEP_WAKEUP_GPIO) {
            woke_from_deep_sleep_ = true;
            gpio_hold_dis(DISPLAY_BACKLIGHT_PIN);
            gpio_deep_sleep_hold_dis();
        }
        InitializeCodecI2c();
        InitializeSpi();
        InitializeDisplay();
        InitializeButtons();
        GetBacklight()->RestoreBrightness();
#if CONFIG_SCREEN_OFF_UNLESS_VOLUME_KEY
        InitializeScreenTimer();
#endif
#if CONFIG_DEEP_SLEEP_IDLE_MINUTES > 0
        InitializeIdleTimer();
#endif
    }

    virtual AudioCodec* GetAudioCodec() override {
        static Es8311AudioCodec audio_codec(
            codec_i2c_bus_, I2C_NUM_0, AUDIO_INPUT_SAMPLE_RATE, AUDIO_OUTPUT_SAMPLE_RATE,
            AUDIO_I2S_GPIO_MCLK, AUDIO_I2S_GPIO_BCLK, AUDIO_I2S_GPIO_WS, AUDIO_I2S_GPIO_DOUT,
            AUDIO_I2S_GPIO_DIN, AUDIO_CODEC_PA_PIN, AUDIO_CODEC_ES8311_ADDR, true, false, 6.0f);
        return &audio_codec;
    }

    virtual Display* GetDisplay() override {
        return display_;
    }

    virtual Backlight* GetBacklight() override {
        static PwmBacklight backlight(DISPLAY_BACKLIGHT_PIN, DISPLAY_BACKLIGHT_OUTPUT_INVERT);
        return &backlight;
    }

    virtual bool GetBatteryLevel(int& level, bool& charging, bool& discharging) override {
        if (!battery_ || !battery_->IsPresent()) {
            return false;
        }
        int soc = battery_->GetBatteryLevel();
        if (soc < 0) {
            return false;
        }
        level = soc;
        // CW2017 reports no charge state and the Passport has no charge-detect
        // GPIO, so report a plain (discharging) reading.
        charging = false;
        discharging = true;
        return true;
    }
};

DECLARE_BOARD(AiPassportBoard);

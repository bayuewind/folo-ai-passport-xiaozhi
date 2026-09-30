#include "passport_display.h"

#include <esp_log.h>

#define TAG "PassportDisplay"

void PassportDisplay::SetupUI() {
    SpiLcdDisplay::SetupUI();

    DisplayLockGuard lock(this);
    auto avatar = std::make_unique<MuseAvatar>();
    if (!avatar->Create(emoji_box_)) {
        ESP_LOGW(TAG, "Muse avatar unavailable, keeping the XiaoZhi emotion");
        return;
    }
    lv_obj_add_flag(emoji_label_, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(emoji_image_, LV_OBJ_FLAG_HIDDEN);
    avatar_ = std::move(avatar);
}

void PassportDisplay::SetEmotion(const char* emotion) {
    // The avatar state comes from the server only; XiaoZhi's own emotions
    // (neutral/thinking/...) would otherwise overwrite it on every state change.
    if (avatar_ != nullptr) {
        return;
    }
    SpiLcdDisplay::SetEmotion(emotion);
}

bool PassportDisplay::SetAvatarState(const std::string& state, int subagents) {
    if (avatar_ == nullptr) {
        return false;
    }
    DisplayLockGuard lock(this);
    return avatar_->SetState(state, subagents);
}

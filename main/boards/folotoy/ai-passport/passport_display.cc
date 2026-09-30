#include "passport_display.h"

#include <esp_log.h>

#define TAG "PassportDisplay"

namespace {

constexpr uint32_t kMainText = 0x151816;
constexpr uint32_t kMutedText = 0x7A827C;
constexpr uint32_t kHintText = 0x9AA19B;
constexpr uint32_t kDotOff = 0xCCD1CC;
constexpr uint32_t kDotOn = 0x151816;
constexpr int kSidePadding = 18;
constexpr int kStatusBarHeight = 36;
constexpr int kReplyLines = 3;

}  // namespace

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

    BuildReplyCard();

    // Vertical page indicator on the right edge (UP/DOWN move between cards).
    pager_ = lv_obj_create(lv_screen_active());
    lv_obj_remove_style_all(pager_);
    lv_obj_set_size(pager_, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
    lv_obj_set_flex_flow(pager_, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_style_pad_row(pager_, 6, 0);
    lv_obj_align(pager_, LV_ALIGN_RIGHT_MID, -9, 0);
    lv_obj_remove_flag(pager_, LV_OBJ_FLAG_SCROLLABLE);
    for (auto& dot : pager_dots_) {
        dot = lv_obj_create(pager_);
        lv_obj_remove_style_all(dot);
        lv_obj_set_style_bg_opa(dot, LV_OPA_COVER, 0);
        lv_obj_set_style_radius(dot, 3, 0);
    }
    card_ = kCardHome;
    lv_obj_add_flag(reply_card_, LV_OBJ_FLAG_HIDDEN);
    UpdatePager();
}

void PassportDisplay::BuildReplyCard() {
    reply_card_ = lv_obj_create(lv_screen_active());
    lv_obj_remove_style_all(reply_card_);
    lv_obj_set_size(reply_card_, LV_HOR_RES - 2 * kSidePadding, LV_SIZE_CONTENT);
    lv_obj_align(reply_card_, LV_ALIGN_TOP_MID, 0, kStatusBarHeight + 14);
    lv_obj_set_flex_flow(reply_card_, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_style_pad_row(reply_card_, 8, 0);
    lv_obj_remove_flag(reply_card_, LV_OBJ_FLAG_SCROLLABLE);

    reply_meta_ = lv_label_create(reply_card_);
    lv_obj_set_style_text_color(reply_meta_, lv_color_hex(kMutedText), 0);
    lv_label_set_text(reply_meta_, "Muse");

    // Three lines at most; the height is fixed in SetReply() and dot mode
    // clips with an ellipsis instead of scrolling.
    reply_text_ = lv_label_create(reply_card_);
    lv_obj_set_width(reply_text_, lv_pct(100));
    lv_label_set_long_mode(reply_text_, LV_LABEL_LONG_DOT);
    lv_obj_set_style_text_color(reply_text_, lv_color_hex(kHintText), 0);
    lv_label_set_text(reply_text_, "还没有回复");

    lv_obj_t* hint = lv_label_create(reply_card_);
    lv_obj_set_style_text_color(hint, lv_color_hex(kHintText), 0);
    lv_obj_set_style_margin_top(hint, 10, 0);
    lv_label_set_text(hint, "单击 OK 重听");
}

void PassportDisplay::SetEmotion(const char* emotion) {
    // The avatar state comes from the server only; XiaoZhi's own emotions
    // (neutral/thinking/...) would otherwise overwrite it on every state change.
    if (avatar_ != nullptr) {
        return;
    }
    SpiLcdDisplay::SetEmotion(emotion);
}

bool PassportDisplay::SetAvatarState(const std::string& state, int subagents,
                                     const std::string& detail) {
    if (avatar_ == nullptr) {
        return false;
    }
    DisplayLockGuard lock(this);
    return avatar_->SetState(state, subagents, detail);
}

void PassportDisplay::SetReply(const std::string& text, const std::string& when) {
    if (reply_card_ == nullptr) {
        return;
    }
    DisplayLockGuard lock(this);
    std::string meta = when.empty() ? "Muse" : "Muse · " + when;
    lv_label_set_text(reply_meta_, meta.c_str());
    lv_label_set_text(reply_text_, text.c_str());
    lv_obj_set_style_text_color(reply_text_, lv_color_hex(kMainText), 0);
    const lv_font_t* font = lv_obj_get_style_text_font(reply_text_, LV_PART_MAIN);
    lv_obj_set_height(reply_text_, kReplyLines * lv_font_get_line_height(font));
}

void PassportDisplay::ShowCard(int card) {
    if (avatar_ == nullptr) {
        return;
    }
    DisplayLockGuard lock(this);
    card_ = (card % kCardCount + kCardCount) % kCardCount;
    if (card_ == kCardHome) {
        lv_obj_add_flag(reply_card_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_remove_flag(emoji_box_, LV_OBJ_FLAG_HIDDEN);
    } else {
        lv_obj_add_flag(emoji_box_, LV_OBJ_FLAG_HIDDEN);
        lv_obj_remove_flag(reply_card_, LV_OBJ_FLAG_HIDDEN);
    }
    UpdatePager();
}

void PassportDisplay::StepCard(int delta) { ShowCard(card_ + delta); }

void PassportDisplay::UpdatePager() {
    for (int i = 0; i < kCardCount; ++i) {
        bool on = i == card_;
        lv_obj_set_size(pager_dots_[i], 5, on ? 12 : 5);
        lv_obj_set_style_bg_color(pager_dots_[i], lv_color_hex(on ? kDotOn : kDotOff), 0);
    }
}

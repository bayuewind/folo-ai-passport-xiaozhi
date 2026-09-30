#ifndef PASSPORT_DISPLAY_H
#define PASSPORT_DISPLAY_H

#include "display/lcd_display.h"
#include "muse_avatar.h"

#include <memory>
#include <string>

// SpiLcdDisplay with the Muse UI:
//  - card 0 "home": the Muse avatar inside emoji_box_ (so the stock preview
//    and activation pages still hide and restore it), state + detail line;
//  - card 1 "latest reply": when and what Muse last replied (3 lines max).
// UP/DOWN switch cards; a vertical page indicator sits on the right edge.
// Status bar and conversation subtitles are unchanged. Without a valid avatar
// partition this behaves exactly like SpiLcdDisplay.
class PassportDisplay : public SpiLcdDisplay {
public:
    enum Card { kCardHome = 0, kCardReply, kCardCount };

    using SpiLcdDisplay::SpiLcdDisplay;

    void SetupUI() override;
    void SetEmotion(const char* emotion) override;

    bool HasAvatar() const { return avatar_ != nullptr; }
    // Returns false for an unknown state or when the avatar is unavailable.
    bool SetAvatarState(const std::string& state, int subagents, const std::string& detail);
    // `when` is a short label such as "14:35"; text is clipped to three lines.
    void SetReply(const std::string& text, const std::string& when);

    void ShowCard(int card);
    void StepCard(int delta);
    int card() const { return card_; }

private:
    void BuildReplyCard();
    void UpdatePager();

    std::unique_ptr<MuseAvatar> avatar_;
    lv_obj_t* reply_card_ = nullptr;
    lv_obj_t* reply_meta_ = nullptr;
    lv_obj_t* reply_text_ = nullptr;
    lv_obj_t* pager_ = nullptr;
    lv_obj_t* pager_dots_[kCardCount] = {};
    int card_ = kCardHome;
};

#endif  // PASSPORT_DISPLAY_H

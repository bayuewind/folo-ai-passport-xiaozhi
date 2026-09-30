#ifndef PASSPORT_DISPLAY_H
#define PASSPORT_DISPLAY_H

#include "display/lcd_display.h"
#include "muse_avatar.h"

#include <memory>
#include <string>

// SpiLcdDisplay with the Muse avatar in place of the XiaoZhi emotion. The
// avatar lives inside emoji_box_, so the stock preview-image and activation
// pages still hide and restore it; status bar and subtitles are unchanged.
// Without a valid avatar partition this behaves exactly like SpiLcdDisplay.
class PassportDisplay : public SpiLcdDisplay {
public:
    using SpiLcdDisplay::SpiLcdDisplay;

    void SetupUI() override;
    void SetEmotion(const char* emotion) override;

    bool HasAvatar() const { return avatar_ != nullptr; }
    // Returns false for an unknown state or when the avatar is unavailable.
    bool SetAvatarState(const std::string& state, int subagents);

private:
    std::unique_ptr<MuseAvatar> avatar_;
};

#endif  // PASSPORT_DISPLAY_H

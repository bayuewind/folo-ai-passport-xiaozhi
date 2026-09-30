#ifndef MUSE_AVATAR_H
#define MUSE_AVATAR_H

#include <esp_partition.h>
#include <lvgl.h>

#include <cstdint>
#include <string>

// Plays the Muse "Hatch" avatar from the `avatar` flash partition produced by
// scripts/muse_avatar/build_avatar_pack.py. Frames are decoded one at a time
// into a single RGB565 buffer, so RAM use is width * height * 2 bytes plus a
// small read chunk regardless of how many frames the partition holds.
//
// The state is pushed by the server (MCP tool `self.avatar.set_state`); the
// avatar never guesses a state on its own and shows "unknown" states greyed
// out instead of falling back to idle.
//
// All methods touch LVGL: call them with the display lock held (or from an
// LVGL timer).
class MuseAvatar {
public:
    MuseAvatar() = default;
    ~MuseAvatar();

    // Builds the avatar widgets inside `parent`. Returns false (and creates
    // nothing) when the partition is missing/invalid or the frame buffer
    // cannot be allocated, so the caller can keep the stock XiaoZhi UI.
    // Text inherits font/colour from the screen so it follows theme and
    // font changes (XiaoZhi swaps in the full CJK font after assets load).
    bool Create(lv_obj_t* parent);

    // Returns false for an unknown state code. `detail` is the optional second
    // line under the state (e.g. "3 分钟 · 2 个子任务"); it never scrolls, so the
    // server keeps it short. Without it, a working state shows the sub-agent count.
    bool SetState(const std::string& state, int subagents, const std::string& detail = "");

    static bool IsKnownState(const std::string& state);

private:
    struct Variant {
        char name[16];
        uint16_t fps;
        uint16_t frame_count;
        uint16_t flags;
        uint16_t reserved;
        uint32_t palette_offset;
        uint32_t table_offset;
    } __attribute__((packed));

    enum Mode { kLoop, kOnce, kStill, kGrey };

    static constexpr int kMaxVariants = 8;
    static constexpr int kChunkSize = 512;

    bool LoadPartition();
    int FindVariant(const char* name) const;
    bool LoadPalette(int variant, bool grey);
    bool DecodeFrame(uint16_t index);
    void ShowFrame();
    void StartVariant(int variant, Mode mode);
    void ApplyState(int state_index, int subagents, const std::string& detail);
    void OnTimer();

    const esp_partition_t* partition_ = nullptr;
    Variant variants_[kMaxVariants] = {};
    int variant_count_ = 0;
    uint16_t width_ = 0;
    uint16_t height_ = 0;

    uint16_t palette_[256] = {};
    uint16_t* frame_ = nullptr;
    uint8_t chunk_[kChunkSize] = {};
    lv_image_dsc_t dsc_ = {};

    lv_obj_t* root_ = nullptr;
    lv_obj_t* image_ = nullptr;
    lv_obj_t* dot_ = nullptr;
    lv_obj_t* label_ = nullptr;
    lv_obj_t* detail_ = nullptr;
    lv_timer_t* timer_ = nullptr;

    int variant_ = -1;
    uint16_t frame_index_ = 0;
    Mode mode_ = kStill;
    int base_state_ = -1;  // state to return to after a one-shot milestone
    int base_subagents_ = 0;
    std::string base_detail_;
};

#endif  // MUSE_AVATAR_H

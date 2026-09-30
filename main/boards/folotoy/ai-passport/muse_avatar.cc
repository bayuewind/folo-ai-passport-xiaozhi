#include "muse_avatar.h"

#include <esp_heap_caps.h>
#include <esp_log.h>

#include <cstring>

#define TAG "MuseAvatar"

namespace {

constexpr char kPartitionLabel[] = "avatar";
constexpr uint16_t kFormatVersion = 1;
constexpr uint16_t kFlagLoop = 1;

struct FileHeader {
    char magic[4];
    uint16_t version;
    uint16_t variant_count;
    uint16_t width;
    uint16_t height;
    uint16_t background;
    uint16_t reserved;
} __attribute__((packed));

struct StateInfo {
    const char* code;
    const char* label;
    const char* variant;
    int mode;  // MuseAvatar::Mode
    uint32_t dot_color;
    bool attention = false;  // needs the user: amber ring around the avatar
};

// Labels follow the Muse desktop pet (desktop-pet/state.cjs). Keep them to
// common characters: MCP text is not covered by the server glyph push.
constexpr uint32_t kGreen = 0x83A87A;
constexpr uint32_t kAmber = 0xD5A553;
constexpr uint32_t kRed = 0xC98274;
constexpr uint32_t kGrey = 0xA0A7AA;
constexpr uint32_t kDetailText = 0x7A827C;

// Mode values mirror MuseAvatar::Mode: 0 loop, 1 once, 2 still, 3 grey.
constexpr StateInfo kStates[] = {
    {"default", "空闲中", "default", 0, kGreen},
    {"working", "正在工作", "working", 0, kGreen},
    {"making_something", "正在制作", "making_something", 0, kGreen},
    {"waiting", "等你回应", "default", 2, kAmber, true},
    {"approval", "需要你批准", "default", 2, kAmber, true},
    {"limited", "用量已耗尽", "default", 2, kRed, true},
    {"syncing", "等待同步", "default", 2, kGrey},
    {"offline", "连接已中断", "default", 3, kRed},
    {"unknown", "状态未知", "default", 3, kGrey},
    {"level_up", "升级啦", "level_up", 1, kGreen},
    {"achievement", "达成成就", "achievement", 1, kGreen},
};

int FindState(const std::string& code) {
    for (int i = 0; i < static_cast<int>(sizeof(kStates) / sizeof(kStates[0])); ++i) {
        if (code == kStates[i].code) {
            return i;
        }
    }
    return -1;
}

// Sequential reader over one frame's byte range in the partition.
class ChunkReader {
public:
    ChunkReader(const esp_partition_t* partition, uint32_t start, uint32_t end, uint8_t* buffer,
                size_t capacity)
        : partition_(partition), next_(start), end_(end), buffer_(buffer), capacity_(capacity) {}

    bool Next(uint8_t& byte) {
        if (pos_ == len_ && !Refill()) {
            return false;
        }
        byte = buffer_[pos_++];
        return true;
    }

    bool AtEnd() const { return pos_ == len_ && next_ == end_; }

private:
    bool Refill() {
        if (next_ >= end_) {
            return false;
        }
        len_ = end_ - next_ < capacity_ ? end_ - next_ : capacity_;
        if (esp_partition_read(partition_, next_, buffer_, len_) != ESP_OK) {
            return false;
        }
        next_ += len_;
        pos_ = 0;
        return true;
    }

    const esp_partition_t* partition_;
    uint32_t next_;
    uint32_t end_;
    uint8_t* buffer_;
    size_t capacity_;
    size_t pos_ = 0;
    size_t len_ = 0;
};

}  // namespace

MuseAvatar::~MuseAvatar() {
    if (timer_ != nullptr) {
        lv_timer_delete(timer_);
    }
    if (root_ != nullptr) {
        lv_obj_delete(root_);
    }
    heap_caps_free(frame_);
}

bool MuseAvatar::IsKnownState(const std::string& state) { return FindState(state) >= 0; }

bool MuseAvatar::LoadPartition() {
    partition_ = esp_partition_find_first(ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_ANY,
                                          kPartitionLabel);
    if (partition_ == nullptr) {
        ESP_LOGW(TAG, "No '%s' partition, avatar disabled", kPartitionLabel);
        return false;
    }
    FileHeader header;
    if (esp_partition_read(partition_, 0, &header, sizeof(header)) != ESP_OK ||
        memcmp(header.magic, "MAVT", 4) != 0 || header.version != kFormatVersion ||
        header.variant_count == 0 || header.variant_count > kMaxVariants || header.width == 0 ||
        header.height == 0 || header.width > 160 || header.height > 160) {
        ESP_LOGW(TAG, "Avatar partition is empty or has an unsupported format");
        return false;
    }
    if (esp_partition_read(partition_, sizeof(header), variants_,
                           sizeof(Variant) * header.variant_count) != ESP_OK) {
        return false;
    }
    // Names are fixed 16-byte fields and a 16-character name has no NUL, so
    // always compare/print them with an explicit length.
    for (int i = 0; i < header.variant_count; ++i) {
        if (variants_[i].frame_count == 0 || variants_[i].fps == 0) {
            ESP_LOGW(TAG, "Variant %d is empty", i);
            return false;
        }
    }
    variant_count_ = header.variant_count;
    width_ = header.width;
    height_ = header.height;
    return true;
}

bool MuseAvatar::Create(lv_obj_t* parent) {
    if (!LoadPartition()) {
        return false;
    }
    const size_t frame_bytes = static_cast<size_t>(width_) * height_ * sizeof(uint16_t);
    frame_ = static_cast<uint16_t*>(
        heap_caps_malloc(frame_bytes, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
    if (frame_ == nullptr) {
        ESP_LOGE(TAG, "Cannot allocate %u B avatar frame buffer",
                 static_cast<unsigned>(frame_bytes));
        return false;
    }
    memset(frame_, 0xFF, frame_bytes);

    dsc_.header.magic = LV_IMAGE_HEADER_MAGIC;
    dsc_.header.flags = LV_IMAGE_FLAGS_MODIFIABLE;
    dsc_.header.cf = LV_COLOR_FORMAT_RGB565;
    dsc_.header.w = width_;
    dsc_.header.h = height_;
    dsc_.header.stride = width_ * sizeof(uint16_t);
    dsc_.data = reinterpret_cast<const uint8_t*>(frame_);
    dsc_.data_size = frame_bytes;

    root_ = lv_obj_create(parent);
    lv_obj_remove_style_all(root_);
    lv_obj_set_size(root_, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
    lv_obj_set_flex_flow(root_, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_flex_align(root_, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_row(root_, 10, 0);
    lv_obj_remove_flag(root_, LV_OBJ_FLAG_SCROLLABLE);

    image_ = lv_image_create(root_);
    lv_image_set_src(image_, &dsc_);
    // Attention ring: an outline drawn outside the (circular) image bounds, so
    // it needs no extra buffer or clipping layer.
    lv_obj_set_style_radius(image_, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_outline_color(image_, lv_color_hex(0xD39A32), 0);
    lv_obj_set_style_outline_pad(image_, 4, 0);
    lv_obj_set_style_outline_width(image_, 0, 0);

    lv_obj_t* caption = lv_obj_create(root_);
    lv_obj_remove_style_all(caption);
    lv_obj_set_size(caption, LV_SIZE_CONTENT, LV_SIZE_CONTENT);
    lv_obj_set_flex_flow(caption, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(caption, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER,
                          LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_column(caption, 6, 0);
    lv_obj_remove_flag(caption, LV_OBJ_FLAG_SCROLLABLE);

    dot_ = lv_obj_create(caption);
    lv_obj_remove_style_all(dot_);
    lv_obj_set_size(dot_, 10, 10);
    lv_obj_set_style_radius(dot_, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_opa(dot_, LV_OPA_COVER, 0);

    label_ = lv_label_create(caption);
    lv_obj_set_style_max_width(label_, 200, 0);
    lv_label_set_long_mode(label_, LV_LABEL_LONG_DOT);

    // Second line: context for the state. Clipped with an ellipsis, never scrolled.
    detail_ = lv_label_create(root_);
    lv_obj_set_style_text_color(detail_, lv_color_hex(kDetailText), 0);
    lv_obj_set_style_max_width(detail_, 204, 0);
    lv_label_set_long_mode(detail_, LV_LABEL_LONG_DOT);
    lv_obj_add_flag(detail_, LV_OBJ_FLAG_HIDDEN);

    timer_ = lv_timer_create(
        [](lv_timer_t* timer) {
            static_cast<MuseAvatar*>(lv_timer_get_user_data(timer))->OnTimer();
        },
        1000, this);
    lv_timer_pause(timer_);

    // Nothing has been received from the server yet.
    ApplyState(FindState("syncing"), 0, "");
    ESP_LOGI(TAG, "Avatar ready: %d variants, %ux%u, frame buffer %u B", variant_count_, width_,
             height_, static_cast<unsigned>(frame_bytes));
    return true;
}

int MuseAvatar::FindVariant(const char* name) const {
    for (int i = 0; i < variant_count_; ++i) {
        if (strncmp(variants_[i].name, name, sizeof(variants_[i].name)) == 0) {
            return i;
        }
    }
    return -1;
}

bool MuseAvatar::LoadPalette(int variant, bool grey) {
    if (esp_partition_read(partition_, variants_[variant].palette_offset, palette_,
                           sizeof(palette_)) != ESP_OK) {
        return false;
    }
    if (grey) {
        // Washed-out greyscale: signals "not a live state" without hiding the character.
        for (auto& color : palette_) {
            int r = ((color >> 11) & 0x1F) << 3;
            int g = ((color >> 5) & 0x3F) << 2;
            int b = (color & 0x1F) << 3;
            int luma = (r * 77 + g * 150 + b * 29) >> 8;
            int v = (luma * 5 + 255 * 3) >> 3;
            color = static_cast<uint16_t>(((v >> 3) << 11) | ((v >> 2) << 5) | (v >> 3));
        }
    }
    return true;
}

bool MuseAvatar::DecodeFrame(uint16_t index) {
    const Variant& variant = variants_[variant_];
    uint32_t range[2];
    if (esp_partition_read(partition_, variant.table_offset + index * sizeof(uint32_t), range,
                           sizeof(range)) != ESP_OK ||
        range[1] < range[0] || range[1] > partition_->size) {
        return false;
    }
    ChunkReader reader(partition_, range[0], range[1], chunk_, sizeof(chunk_));
    const uint32_t total = static_cast<uint32_t>(width_) * height_;
    uint32_t pixel = 0;
    while (pixel < total) {
        uint32_t token = 0;
        uint8_t byte;
        for (int shift = 0;; shift += 7) {
            if (shift > 21 || !reader.Next(byte)) {
                return false;
            }
            token |= static_cast<uint32_t>(byte & 0x7F) << shift;
            if (!(byte & 0x80)) {
                break;
            }
        }
        const uint32_t count = token >> 2;
        if (count > total - pixel) {
            return false;
        }
        switch (token & 3) {
            case 0:  // unchanged since the previous frame
                break;
            case 1:
                for (uint32_t i = 0; i < count; ++i) {
                    if (!reader.Next(byte)) {
                        return false;
                    }
                    frame_[pixel + i] = palette_[byte];
                }
                break;
            case 2:
                if (!reader.Next(byte)) {
                    return false;
                }
                for (uint32_t i = 0; i < count; ++i) {
                    frame_[pixel + i] = palette_[byte];
                }
                break;
            default:
                return false;
        }
        pixel += count;
    }
    return reader.AtEnd();
}

void MuseAvatar::ShowFrame() {
    // The buffer is reused for every frame. This build has no LVGL image cache
    // (CONFIG_LV_CACHE_DEF_SIZE=0) and set_src invalidates, like LvglGif does.
    lv_image_set_src(image_, &dsc_);
}

void MuseAvatar::StartVariant(int variant, Mode mode) {
    lv_timer_pause(timer_);
    variant_ = variant;
    mode_ = mode;
    frame_index_ = 0;
    // Frame 0 is a key frame, so switching variants never shows stale pixels.
    if (!LoadPalette(variant, mode == kGrey) || !DecodeFrame(0)) {
        ESP_LOGE(TAG, "Failed to decode %.16s", variants_[variant].name);
        return;
    }
    ShowFrame();
    if ((mode == kLoop || mode == kOnce) && variants_[variant].frame_count > 1) {
        lv_timer_set_period(timer_, 1000 / variants_[variant].fps);
        lv_timer_reset(timer_);
        lv_timer_resume(timer_);
    }
}

void MuseAvatar::OnTimer() {
    const Variant& variant = variants_[variant_];
    uint16_t next = frame_index_ + 1;
    if (next >= variant.frame_count) {
        if (mode_ == kOnce || !(variant.flags & kFlagLoop)) {
            // Milestone finished: go back to whatever the server last set.
            ApplyState(base_state_ >= 0 ? base_state_ : FindState("default"), base_subagents_,
                       base_detail_);
            return;
        }
        next = 0;
    }
    if (!DecodeFrame(next)) {
        ESP_LOGE(TAG, "Failed to decode %.16s frame %u", variant.name, next);
        lv_timer_pause(timer_);
        return;
    }
    frame_index_ = next;
    ShowFrame();
}

void MuseAvatar::ApplyState(int state_index, int subagents, const std::string& detail) {
    const StateInfo& info = kStates[state_index];
    if (info.mode != kOnce) {
        base_state_ = state_index;
        base_subagents_ = subagents;
        base_detail_ = detail;
    }

    lv_obj_set_style_bg_color(dot_, lv_color_hex(info.dot_color), 0);
    lv_label_set_text(label_, info.label);
    lv_obj_set_style_outline_width(image_, info.attention ? 4 : 0, 0);
    std::string line = detail;
    if (line.empty() && state_index == FindState("working") && subagents > 0) {
        line = std::to_string(subagents) + " 个子任务";
    }
    if (line.empty()) {
        lv_obj_add_flag(detail_, LV_OBJ_FLAG_HIDDEN);
    } else {
        lv_label_set_text(detail_, line.c_str());
        lv_obj_remove_flag(detail_, LV_OBJ_FLAG_HIDDEN);
    }

    int variant = FindVariant(info.variant);
    if (variant < 0) {
        // Older/partial pack: fall back to the default loop's first frame.
        variant = FindVariant("default");
        if (variant < 0) {
            return;
        }
        StartVariant(variant, info.mode == kGrey ? kGrey : kStill);
        return;
    }
    StartVariant(variant, static_cast<Mode>(info.mode));
}

bool MuseAvatar::SetState(const std::string& state, int subagents, const std::string& detail) {
    int index = FindState(state);
    if (index < 0 || root_ == nullptr) {
        return false;
    }
    ApplyState(index, subagents < 0 ? 0 : subagents, detail);
    return true;
}

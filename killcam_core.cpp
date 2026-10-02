// killcam_core.cpp — native capture+feed core for KillCam+.
//
// Owns the high-rate video loop in C++ (no GIL): DXGI desktop duplication
// -> BGRX-to-BGR24 (+ optional bilinear downscale, optional region crop
// with top-left pad) -> ffmpeg stdin via raw Win32 pipes. ffmpeg stdout
// (fragmented MP4) is pumped into a Python-drained byte queue. Python keeps
// the fragment ring, save/remux, audio, UI and settings.
//
// Build:  py -3.11 setup.py build_ext --inplace   (MSVC + Windows SDK)
// Output: killcam_core.cp311-win_amd64.pyd next to recorder.py.
//
// v1 scope: full-output or single-region capture, constant pipe geometry,
// every-slot feeding (GPU encoders require dense input; matches the Python
// raw path). libx264 sparse-skip is intentionally not replicated.

#define NOMINMAX
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <d3d11.h>
#include <dxgi1_2.h>
#include <wrl/client.h>

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <deque>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace py = pybind11;
using Microsoft::WRL::ComPtr;

namespace {

// ---- QPC clock (same tick base as time.monotonic on Windows) ----
inline uint64_t qpc_now() {
    LARGE_INTEGER v;
    QueryPerformanceCounter(&v);
    return static_cast<uint64_t>(v.QuadPart);
}
inline uint64_t qpc_freq() {
    LARGE_INTEGER f;
    QueryPerformanceFrequency(&f);
    return static_cast<uint64_t>(f.QuadPart);
}

// ---- argv -> CreateProcess command line (quotes args with spaces) ----
std::wstring build_cmdline(const std::vector<std::string> &argv) {
    std::wstring out;
    for (size_t i = 0; i < argv.size(); ++i) {
        if (i) out += L' ';
        std::string a = argv[i];
        bool quote = a.empty() || a.find_first_of(" \t\"") != std::string::npos;
        std::wstring w(a.begin(), a.end());
        if (!quote) {
            out += w;
            continue;
        }
        out += L'"';
        for (wchar_t c : w) {
            if (c == L'"') out += L"\\";
            out += c;
        }
        out += L'"';
    }
    return out;
}

// ---- BGRX (pitch-aware) -> packed BGR24, optional sub-rect ----
// dst_pitch lets a small region land top-left inside a larger canvas
// (black pad), matching the Python _pad_to_output placement exactly.
void convert_bgrx_to_bgr24(const uint8_t *src, int src_pitch,
                            int sx, int sy, int w, int h,
                            uint8_t *dst, int dst_pitch) {
    for (int y = 0; y < h; ++y) {
        const uint8_t *row = src + (sy + y) * src_pitch + sx * 4;
        uint8_t *d = dst + static_cast<size_t>(y) * dst_pitch;
        for (int x = 0; x < w; ++x) {
            d[0] = row[0];
            d[1] = row[1];
            d[2] = row[2];
            d += 3;
            row += 4;
        }
    }
}

// ---- bilinear BGR24 -> BGR24 downscale (also correct for upscale) ----
void scale_bilinear_bgr24(const uint8_t *src, int sw, int sh,
                           uint8_t *dst, int dw, int dh) {
    const float x_ratio = static_cast<float>(sw) / dw;
    const float y_ratio = static_cast<float>(sh) / dh;
    for (int y = 0; y < dh; ++y) {
        float sy = (y + 0.5f) * y_ratio - 0.5f;
        int y0 = static_cast<int>(sy);
        float fy = sy - y0;
        if (y0 < 0) { y0 = 0; fy = 0.0f; }
        int y1 = y0 + 1;
        if (y1 >= sh) y1 = sh - 1;
        for (int x = 0; x < dw; ++x) {
            float sx = (x + 0.5f) * x_ratio - 0.5f;
            int x0 = static_cast<int>(sx);
            float fx = sx - x0;
            if (x0 < 0) { x0 = 0; fx = 0.0f; }
            int x1 = x0 + 1;
            if (x1 >= sw) x1 = sw - 1;
            uint8_t *d = dst + (static_cast<size_t>(y) * dw + x) * 3;
            for (int c = 0; c < 3; ++c) {
                float p00 = src[(static_cast<size_t>(y0) * sw + x0) * 3 + c];
                float p10 = src[(static_cast<size_t>(y0) * sw + x1) * 3 + c];
                float p01 = src[(static_cast<size_t>(y1) * sw + x0) * 3 + c];
                float p11 = src[(static_cast<size_t>(y1) * sw + x1) * 3 + c];
                float v = (p00 * (1 - fx) + p10 * fx) * (1 - fy)
                        + (p01 * (1 - fx) + p11 * fx) * fy;
                d[c] = static_cast<uint8_t>(v + 0.5f);
            }
        }
    }
}

bool write_all(HANDLE h, const uint8_t *data, size_t len) {
    size_t off = 0;
    while (off < len) {
        DWORD chunk = static_cast<DWORD>((len - off > 1 << 20) ? (1 << 20) : (len - off));
        DWORD wrote = 0;
        if (!WriteFile(h, data + off, chunk, &wrote, nullptr) || wrote == 0)
            return false;  // broken pipe: ffmpeg exited
        off += wrote;
    }
    return true;
}

}  // namespace

class RecorderCore {
public:
    RecorderCore() = default;
    ~RecorderCore() { stop(); }

    RecorderCore(const RecorderCore &) = delete;
    RecorderCore &operator=(const RecorderCore &) = delete;

    static std::vector<std::array<int, 4>> list_outputs() {
        std::vector<std::array<int, 4>> out;
        ComPtr<IDXGIFactory1> factory;
        if (FAILED(CreateDXGIFactory1(__uuidof(IDXGIFactory1),
                                      reinterpret_cast<void **>(factory.GetAddressOf()))))
            return out;
        for (UINT a = 0;; ++a) {
            ComPtr<IDXGIAdapter1> adapter;
            if (factory->EnumAdapters1(a, &adapter) == DXGI_ERROR_NOT_FOUND)
                break;
            for (UINT o = 0;; ++o) {
                ComPtr<IDXGIOutput> output;
                if (adapter->EnumOutputs(o, &output) == DXGI_ERROR_NOT_FOUND)
                    break;
                DXGI_OUTPUT_DESC d{};
                if (FAILED(output->GetDesc(&d)))
                    continue;
                if (!d.AttachedToDesktop)
                    continue;
                out.push_back({d.DesktopCoordinates.left, d.DesktopCoordinates.top,
                               d.DesktopCoordinates.right - d.DesktopCoordinates.left,
                               d.DesktopCoordinates.bottom - d.DesktopCoordinates.top});
            }
        }
        return out;
    }

    static uint64_t qpc_now_s() { return qpc_now(); }
    static uint64_t qpc_frequency_s() { return qpc_freq(); }

    // region: (l, t, r, b) output-relative, or empty for full output.
    bool start(const std::vector<std::string> &argv,
               int native_w, int native_h,
               int out_w, int out_h, int fps,
               int output_index,
               const std::vector<int> &region) {
        stop();
        if (argv.empty() || native_w < 16 || native_h < 16
                || out_w < 16 || out_h < 16 || fps < 1) {
            last_error_ = "bad start arguments";
            return false;
        }
        fps_ = fps;
        native_w_ = native_w;
        native_h_ = native_h;
        out_w_ = out_w;
        out_h_ = out_h;
        output_index_ = output_index;
        region_ = region;
        work_output_ = output_index;
        work_region_ = region;
        if (!open_duplication(output_index)) {
            close_child();
            return false;  // last_error_ set inside
        }
        frame_bytes_ = static_cast<size_t>(out_w_) * out_h_ * 3;
        work_.assign(static_cast<size_t>(native_w_) * native_h_ * 3, 0);
        staged_.assign(frame_bytes_, 0);
        last_fed_.assign(frame_bytes_, 0);
        {
            std::lock_guard<std::mutex> lk(latest_mu_);
            latest_.assign(frame_bytes_, 0);
            have_latest_ = false;
        }
        if (!spawn_child(argv)) {
            close_duplication();
            return false;
        }
        running_.store(true);
        try {
            cap_thread_ = std::thread(&RecorderCore::capture_loop, this);
            read_thread_ = std::thread(&RecorderCore::read_loop, this);
        } catch (const std::exception &e) {
            last_error_ = e.what();
            running_.store(false);
            join_threads();
            close_child();
            close_duplication();
            return false;
        }
        return true;
    }

    // Switch duplication target mid-run (ffmpeg keeps running: pipe
    // geometry is constant by construction). Same region convention;
    // the capture thread adopts it on its next tick via work copies.
    bool reconfigure(int output_index, const std::vector<int> &region) {
        std::lock_guard<std::mutex> lk(cfg_mu_);
        output_index_ = output_index;
        region_ = region;
        pending_reconf_.store(true);
        return true;
    }
    void stop() {
        running_.store(false);
        // Closing stdin lets ffmpeg finalize and exit (mirrors the
        // Python stop path); reader then sees EOF and finishes.
        if (child_stdin_ && child_stdin_ != INVALID_HANDLE_VALUE) {
            CloseHandle(child_stdin_);
            child_stdin_ = nullptr;
        }
        join_threads();
        close_child();
        close_duplication();
    }

    bool running() const { return running_.load(); }

    bool child_alive() {
        if (!child_) return false;
        DWORD code = 0;
        if (!GetExitCodeProcess(child_, &code))
            return false;
        return code == STILL_ACTIVE;
    }

    py::bytes read_stdout(size_t max_bytes) {
        std::lock_guard<std::mutex> lk(q_mu_);
        if (out_q_.empty())
            return py::bytes();
        size_t n = 0;
        for (const auto &c : out_q_) {
            n += c.size();
            if (n >= max_bytes) break;
        }
        std::string out;
        out.reserve(n);
        while (!out_q_.empty() && out.size() < max_bytes) {
            auto &front = out_q_.front();
            size_t take = front.size();
            if (out.size() + take > max_bytes)
                take = max_bytes - out.size();
            out.append(reinterpret_cast<const char *>(front.data()), take);
            if (take < front.size())
                front.erase(front.begin(), front.begin() + take);
            else
                out_q_.pop_front();
        }
        return py::bytes(out);
    }

    // [(qpc_ticks, fresh)] drained oldest-first.
    std::vector<std::pair<uint64_t, bool>> drain_feed_log() {
        std::lock_guard<std::mutex> lk(log_mu_);
        std::vector<std::pair<uint64_t, bool>> out;
        out.reserve(feed_log_.size());
        for (auto &e : feed_log_) out.push_back(e);
        feed_log_.clear();
        return out;
    }

    // Latest target-size BGR24 frame (Python throttles reads at ~4 Hz).
    py::bytes get_latest_frame() {
        std::lock_guard<std::mutex> lk(latest_mu_);
        if (!have_latest_ || latest_.empty())
            return py::bytes();
        return py::bytes(reinterpret_cast<const char *>(latest_.data()),
                         latest_.size());
    }

    std::string last_error() const { return last_error_; }

private:
    bool open_duplication(int output_index) {
        close_duplication();
        ComPtr<IDXGIFactory1> factory;
        if (FAILED(CreateDXGIFactory1(__uuidof(IDXGIFactory1),
                                      reinterpret_cast<void **>(factory.GetAddressOf())))) {
            last_error_ = "CreateDXGIFactory1 failed";
            return false;
        }
        ComPtr<IDXGIAdapter1> adapter;
        ComPtr<IDXGIOutput> output;
        int seen = -1;
        bool found = false;
        for (UINT a = 0; !found; ++a) {
            if (factory->EnumAdapters1(a, &adapter) == DXGI_ERROR_NOT_FOUND)
                break;
            for (UINT o = 0;; ++o) {
                ComPtr<IDXGIOutput> cand;
                if (adapter->EnumOutputs(o, &cand) == DXGI_ERROR_NOT_FOUND)
                    break;
                DXGI_OUTPUT_DESC d{};
                if (FAILED(cand->GetDesc(&d)) || !d.AttachedToDesktop)
                    continue;
                if (++seen == output_index) {
                    output = cand;
                    found = true;
                    break;
                }
            }
            if (!found) adapter.Reset();
        }
        if (!found) {
            last_error_ = "output index out of range";
            return false;
        }
        // Device must live on the output's adapter.
        ComPtr<IDXGIAdapter> plain_adapter;
        adapter.As(&plain_adapter);
        UINT flags = D3D11_CREATE_DEVICE_BGRA_SUPPORT;
        D3D_FEATURE_LEVEL fl = D3D_FEATURE_LEVEL_11_0;
        HRESULT hr = D3D11CreateDevice(
            plain_adapter.Get(), D3D_DRIVER_TYPE_UNKNOWN, nullptr, flags,
            &fl, 1, D3D11_SDK_VERSION, &device_, nullptr, &context_);
        if (FAILED(hr)) {
            last_error_ = "D3D11CreateDevice failed";
            return false;
        }
        ComPtr<IDXGIOutput1> out1;
        if (FAILED(output.As(&out1))) {
            last_error_ = "output is not IDXGIOutput1";
            close_duplication();
            return false;
        }
        if (FAILED(out1->DuplicateOutput(device_.Get(), &dup_))) {
            last_error_ = "DuplicateOutput failed (session held elsewhere?)";
            close_duplication();
            return false;
        }
        return true;
    }

    void close_duplication() {
        if (dup_) {
            try { dup_->ReleaseFrame(); } catch (...) {}
            dup_.Reset();
        }
        context_.Reset();
        device_.Reset();
    }

    bool spawn_child(const std::vector<std::string> &argv) {
        SECURITY_ATTRIBUTES sa{};
        sa.nLength = sizeof(sa);
        sa.bInheritHandle = TRUE;
        HANDLE in_r = nullptr, in_w = nullptr;
        HANDLE out_r = nullptr, out_w = nullptr;
        if (!CreatePipe(&in_r, &in_w, &sa, 1 << 20)) {
            last_error_ = "stdin pipe failed";
            return false;
        }
        if (!CreatePipe(&out_r, &out_w, &sa, 1 << 20)) {
            CloseHandle(in_r);
            CloseHandle(in_w);
            last_error_ = "stdout pipe failed";
            return false;
        }
        // Our ends stay private; the child's ends inherit.
        SetHandleInformation(in_w, HANDLE_FLAG_INHERIT, 0);
        SetHandleInformation(out_r, HANDLE_FLAG_INHERIT, 0);
        HANDLE nul = CreateFileW(L"NUL", GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE,
                                 &sa, OPEN_EXISTING, 0, nullptr);
        STARTUPINFOW si{};
        si.cb = sizeof(si);
        si.dwFlags = STARTF_USESTDHANDLES;
        si.hStdInput = in_r;
        si.hStdOutput = out_w;
        si.hStdError = (nul != INVALID_HANDLE_VALUE) ? nul : GetStdHandle(STD_ERROR_HANDLE);
        PROCESS_INFORMATION pi{};
        std::wstring cmd = build_cmdline(argv);
        BOOL ok = CreateProcessW(nullptr, cmd.data(), nullptr, nullptr, TRUE,
                                 CREATE_NO_WINDOW, nullptr, nullptr, &si, &pi);
        CloseHandle(in_r);
        CloseHandle(out_w);
        if (nul != INVALID_HANDLE_VALUE) CloseHandle(nul);
        if (!ok) {
            CloseHandle(in_w);
            CloseHandle(out_r);
            last_error_ = "CreateProcess(ffmpeg) failed";
            return false;
        }
        CloseHandle(pi.hThread);
        child_ = pi.hProcess;
        child_stdin_ = in_w;
        child_stdout_ = out_r;
        return true;
    }

    void close_child() {
        if (child_stdin_ && child_stdin_ != INVALID_HANDLE_VALUE) {
            CloseHandle(child_stdin_);
            child_stdin_ = nullptr;
        }
        if (child_stdout_ && child_stdout_ != INVALID_HANDLE_VALUE) {
            CloseHandle(child_stdout_);
            child_stdout_ = nullptr;
        }
        if (child_) {
            DWORD code = 0;
            if (GetExitCodeProcess(child_, &code) && code == STILL_ACTIVE) {
                // Stuck child (mirrors the Python 5 s reap, shortened:
                // stop() already closed stdin, so a live child here is wedged).
                TerminateProcess(child_, 1);
            }
            CloseHandle(child_);
            child_ = nullptr;
        }
    }

    void join_threads() {
        if (cap_thread_.joinable()) {
            try { cap_thread_.join(); } catch (...) {}
        }
        if (read_thread_.joinable()) {
            try { read_thread_.join(); } catch (...) {}
        }
    }

    // Resolve active capture rect (output pixels) from work copies.
    // Region is output-relative (l, t, r, b); empty = full output.
    void resolve_rect(int &sx, int &sy, int &sw, int &sh) {
        sx = 0;
        sy = 0;
        sw = native_w_;
        sh = native_h_;
        if (work_region_.size() == 4) {
            int l = work_region_[0], t = work_region_[1];
            int r = work_region_[2], b = work_region_[3];
            if (l < 0) l = 0;
            if (t < 0) t = 0;
            if (r > native_w_) r = native_w_;
            if (b > native_h_) b = native_h_;
            if (r - l >= 16 && b - t >= 16) {
                sx = l;
                sy = t;
                sw = r - l;
                sh = b - t;
            }
        }
    }

    void capture_loop() {
        const uint64_t freq = qpc_freq();
        const uint64_t tick = freq / static_cast<uint64_t>(fps_ > 0 ? fps_ : 60);
        uint64_t next = qpc_now();
        bool have_frame = false;

        // Staging texture (CPU-readable) for the full output.
        D3D11_TEXTURE2D_DESC sd{};
        sd.Width = static_cast<UINT>(native_w_);
        sd.Height = static_cast<UINT>(native_h_);
        sd.MipLevels = 1;
        sd.ArraySize = 1;
        sd.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
        sd.SampleDesc.Count = 1;
        sd.Usage = D3D11_USAGE_STAGING;
        sd.CPUAccessFlags = D3D11_CPU_ACCESS_READ;

        while (running_.load()) {
            if (pending_reconf_.exchange(false)) {
                int oi;
                std::vector<int> rg;
                {
                    std::lock_guard<std::mutex> lk(cfg_mu_);
                    oi = output_index_;
                    rg = region_;
                }
                work_output_ = oi;
                work_region_ = rg;
                open_duplication(work_output_);
                have_frame = false;
            }
            uint64_t now = qpc_now();
            if (now < next) {
                uint64_t ms = (next - now) * 1000 / freq;
                if (ms > 0) {
                    if (ms > 50) ms = 50;
                    Sleep(static_cast<DWORD>(ms));
                    continue;
                }
            }
            next += tick;
            if (next < now - freq / 2)
                next = now;  // cap catch-up burst (mirrors Python pacing)

            bool fresh = false;
            bool fed = false;
            if (!dup_) {
                // No session (reconfigure failed): hold cadence, feed last.
                fed = !last_fed_.empty();
            } else {
                ComPtr<IDXGIResource> res;
                DXGI_OUTDUPL_FRAME_INFO info{};
                HRESULT hr = dup_->AcquireNextFrame(100, &info, &res);
                if (hr == S_OK) {
                    ComPtr<ID3D11Texture2D> tex;
                    if (SUCCEEDED(res.As(&tex))) {
                        ComPtr<ID3D11Texture2D> stage;
                        if (SUCCEEDED(device_->CreateTexture2D(&sd, nullptr, &stage))) {
                            context_->CopyResource(stage.Get(), tex.Get());
                            D3D11_MAPPED_SUBRESOURCE map{};
                            if (SUCCEEDED(context_->Map(stage.Get(), 0, D3D11_MAP_READ, 0, &map))) {
                                int sx, sy, sw, sh;
                                resolve_rect(sx, sy, sw, sh);
                                if (sw == native_w_ && sh == native_h_
                                        && out_w_ == native_w_ && out_h_ == native_h_) {
                                    convert_bgrx_to_bgr24(
                                        static_cast<const uint8_t *>(map.pData),
                                        static_cast<int>(map.RowPitch),
                                        0, 0, native_w_, native_h_, staged_.data(),
                                        native_w_ * 3);
                                } else if (sw == out_w_ && sh == out_h_) {
                                    // Exact-size crop (padded upstream if smaller).
                                    convert_bgrx_to_bgr24(
                                        static_cast<const uint8_t *>(map.pData),
                                        static_cast<int>(map.RowPitch),
                                        sx, sy, sw, sh, staged_.data(),
                                        out_w_ * 3);
                                } else {
                                    // Crop (+ top-left black pad when the
                                    // region is smaller than native) then
                                    // scale to the target (memcpy when the
                                    // sizes already match: same content).
                                    std::fill(work_.begin(), work_.end(), 0);
                                    convert_bgrx_to_bgr24(
                                        static_cast<const uint8_t *>(map.pData),
                                        static_cast<int>(map.RowPitch),
                                        sx, sy, sw, sh, work_.data(),
                                        native_w_ * 3);
                                    if (native_w_ == out_w_ && native_h_ == out_h_)
                                        staged_.assign(work_.begin(), work_.end());
                                    else
                                        scale_bilinear_bgr24(work_.data(), native_w_, native_h_,
                                                             staged_.data(), out_w_, out_h_);
                                }
                                context_->Unmap(stage.Get(), 0);
                                fresh = !have_frame
                                    || std::memcmp(staged_.data(), last_fed_.data(),
                                                   frame_bytes_) != 0;
                                have_frame = true;
                                fed = true;
                            }
                        }
                    }
                    try { dup_->ReleaseFrame(); } catch (...) {}
                } else if (hr == DXGI_ERROR_WAIT_TIMEOUT) {
                    fed = have_frame;  // static scene: re-feed last (GPU clock)
                } else {
                    // Session lost (mode change / orphan): drop it; the
                    // supervisor reconfigures, cadence holds meanwhile.
                    close_duplication();
                    fed = have_frame;
                }
            }
            if (fed && child_stdin_) {
                if (fresh) {
                    last_fed_.assign(staged_.begin(), staged_.end());
                    std::lock_guard<std::mutex> lk(latest_mu_);
                    latest_.assign(staged_.begin(), staged_.end());
                    have_latest_ = true;
                }
                if (write_all(child_stdin_, last_fed_.data(), frame_bytes_)) {
                    std::lock_guard<std::mutex> lk(log_mu_);
                    feed_log_.emplace_back(qpc_now(), fresh);
                } else {
                    break;  // ffmpeg exited: stop feeding
                }
            }
        }
    }

    void read_loop() {
        std::vector<uint8_t> buf(1 << 17);
        while (running_.load()) {
            if (!child_stdout_) break;
            DWORD got = 0;
            BOOL ok = ReadFile(child_stdout_, buf.data(),
                               static_cast<DWORD>(buf.size()), &got, nullptr);
            if (!ok || got == 0)
                break;  // EOF / broken pipe: encoder exited
            std::lock_guard<std::mutex> lk(q_mu_);
            out_q_.emplace_back(buf.begin(), buf.begin() + got);
        }
    }

    // ---- config (cfg_mu_ guards output/region swaps) ----
    std::mutex cfg_mu_;
    int fps_ = 60;
    int native_w_ = 1920, native_h_ = 1080;
    int out_w_ = 1920, out_h_ = 1080;
    int output_index_ = 0;
    std::vector<int> region_;
    std::atomic<bool> pending_reconf_{false};
    // Working copies owned by the capture thread (no data race with
    // reconfigure, which only touches output_index_/region_ under lock).
    int work_output_ = 0;
    std::vector<int> work_region_;
    size_t frame_bytes_ = 0;

    // ---- workers ----
    std::atomic<bool> running_{false};
    std::thread cap_thread_;
    std::thread read_thread_;
    std::vector<uint8_t> work_;    // native-size BGR24 scratch (pad target)
    std::vector<uint8_t> staged_;  // target-size frame being fed
    std::vector<uint8_t> last_fed_;  // last fed bytes (dup check + re-feed)

    // ---- child ----
    HANDLE child_ = nullptr;
    HANDLE child_stdin_ = nullptr;
    HANDLE child_stdout_ = nullptr;

    // ---- duplication ----
    ComPtr<ID3D11Device> device_;
    ComPtr<ID3D11DeviceContext> context_;
    ComPtr<IDXGIOutputDuplication> dup_;

    // ---- handoff ----
    std::mutex q_mu_;
    std::deque<std::vector<uint8_t>> out_q_;
    std::mutex log_mu_;
    std::deque<std::pair<uint64_t, bool>> feed_log_;
    std::mutex latest_mu_;
    std::vector<uint8_t> latest_;
    bool have_latest_ = false;

    std::string last_error_;
};

PYBIND11_MODULE(killcam_core, m) {
    m.doc() = "KillCam+ native capture+feed core (DXGI -> ffmpeg pipes)";
    py::class_<RecorderCore>(m, "RecorderCore")
        .def(py::init<>())
        .def("start", &RecorderCore::start,
             py::arg("argv"), py::arg("native_w"), py::arg("native_h"),
             py::arg("out_w"), py::arg("out_h"), py::arg("fps"),
             py::arg("output_index"), py::arg("region") = std::vector<int>(),
             py::call_guard<py::gil_scoped_release>())
        .def("reconfigure", &RecorderCore::reconfigure,
             py::arg("output_index"), py::arg("region") = std::vector<int>())
        .def("stop", &RecorderCore::stop,
             py::call_guard<py::gil_scoped_release>())
        .def("running", &RecorderCore::running)
        .def("child_alive", &RecorderCore::child_alive)
        .def("read_stdout", &RecorderCore::read_stdout,
             py::arg("max_bytes") = (1 << 20))
        .def("drain_feed_log", &RecorderCore::drain_feed_log)
        .def("get_latest_frame", &RecorderCore::get_latest_frame)
        .def("last_error", &RecorderCore::last_error)
        .def_static("list_outputs", &RecorderCore::list_outputs)
        .def_static("qpc_now", &RecorderCore::qpc_now_s)
        .def_static("qpc_frequency", &RecorderCore::qpc_frequency_s);
}

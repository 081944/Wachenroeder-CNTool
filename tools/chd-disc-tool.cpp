#include <libchdr/chd.h>
extern "C" {
#include <libchdr/cdrom.h>
}

#include <algorithm>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace fs = std::filesystem;

struct TrackInfo {
    int number = 0;
    std::string type;
    std::string subtype;
    int frames = 0;
    int pregap = 0;
    std::string pregapType;
    std::string pregapSub;
    int postgap = 0;
    uint64_t firstFrame = 0;
};

static void fail(const std::string &message) {
    throw std::runtime_error(message);
}

static std::string jsonEscape(std::string_view value) {
    std::string result;
    for (const unsigned char c : value) {
        switch (c) {
        case '\\': result += "\\\\"; break;
        case '"': result += "\\\""; break;
        case '\n': result += "\\n"; break;
        case '\r': result += "\\r"; break;
        case '\t': result += "\\t"; break;
        default: result += static_cast<char>(c); break;
        }
    }
    return result;
}

static bool startsWith(std::string_view value, std::string_view prefix) {
    return value.size() >= prefix.size() && value.substr(0, prefix.size()) == prefix;
}

static bool readInt(std::string_view text, size_t &position, std::string_view prefix, int &value) {
    if (!startsWith(text.substr(position), prefix)) return false;
    position += prefix.size();
    const size_t begin = position;
    while (position < text.size() && text[position] >= '0' && text[position] <= '9') ++position;
    if (begin == position) return false;
    value = std::stoi(std::string(text.substr(begin, position - begin)));
    return true;
}

static bool readWord(std::string_view text, size_t &position, std::string_view prefix, std::string &value) {
    if (!startsWith(text.substr(position), prefix)) return false;
    position += prefix.size();
    const size_t begin = position;
    while (position < text.size() && text[position] != ' ') ++position;
    if (begin == position) return false;
    value.assign(text.substr(begin, position - begin));
    return true;
}

static TrackInfo parseTrack(std::string_view metadata) {
    TrackInfo track;
    size_t position = 0;
    if (!readInt(metadata, position, "TRACK:", track.number) ||
        !readWord(metadata, position, " TYPE:", track.type) ||
        !readWord(metadata, position, " SUBTYPE:", track.subtype) ||
        !readInt(metadata, position, " FRAMES:", track.frames)) {
        fail("Unsupported CHD track metadata: " + std::string(metadata));
    }
    if (position < metadata.size()) {
        if (!readInt(metadata, position, " PREGAP:", track.pregap) ||
            !readWord(metadata, position, " PGTYPE:", track.pregapType) ||
            !readWord(metadata, position, " PGSUB:", track.pregapSub) ||
            !readInt(metadata, position, " POSTGAP:", track.postgap)) {
            fail("Malformed CHT2 metadata: " + std::string(metadata));
        }
    }
    return track;
}

static std::vector<TrackInfo> readTracks(chd_file *file) {
    std::vector<TrackInfo> tracks;
    std::vector<char> buffer(512);
    for (uint32_t index = 0;; ++index) {
        uint32_t resultLength = 0;
        uint32_t resultTag = 0;
        uint8_t resultFlags = 0;
        chd_error error = chd_get_metadata(file, CDROM_TRACK_METADATA2_TAG, index,
                                            buffer.data(), static_cast<uint32_t>(buffer.size()),
                                            &resultLength, &resultTag, &resultFlags);
        if (error == CHDERR_METADATA_NOT_FOUND) break;
        if (error == CHDERR_INVALID_METADATA_SIZE) {
            buffer.resize(resultLength + 1);
            error = chd_get_metadata(file, CDROM_TRACK_METADATA2_TAG, index,
                                     buffer.data(), static_cast<uint32_t>(buffer.size()),
                                     &resultLength, &resultTag, &resultFlags);
        }
        if (error != CHDERR_NONE) fail("Cannot read CHD metadata: " + std::string(chd_error_string(error)));
        buffer[resultLength] = '\0';
        TrackInfo track = parseTrack(std::string_view(buffer.data(), resultLength));
        track.firstFrame = tracks.empty() ? 0 : tracks.back().firstFrame + tracks.back().frames;
        tracks.push_back(std::move(track));
    }
    if (tracks.empty()) fail("CHD contains no CHT2 track metadata");
    return tracks;
}

static void writeMetadata(const fs::path &path, const chd_header *header, const std::vector<TrackInfo> &tracks) {
    std::ofstream output(path, std::ios::binary);
    if (!output) fail("Cannot write metadata: " + path.string());
    output << "{\n"
           << "  \"format\": \"wachenroeder-chd-extract-v1\",\n"
           << "  \"logical_bytes\": " << header->logicalbytes << ",\n"
           << "  \"hunk_bytes\": " << header->hunkbytes << ",\n"
           << "  \"unit_bytes\": " << header->unitbytes << ",\n"
           << "  \"tracks\": [\n";
    for (size_t i = 0; i < tracks.size(); ++i) {
        const auto &track = tracks[i];
        output << "    {\"number\": " << track.number
               << ", \"type\": \"" << jsonEscape(track.type)
               << "\", \"subtype\": \"" << jsonEscape(track.subtype)
               << "\", \"frames\": " << track.frames
               << ", \"pregap\": " << track.pregap
               << ", \"pregap_type\": \"" << jsonEscape(track.pregapType)
               << "\", \"pregap_sub\": \"" << jsonEscape(track.pregapSub)
               << "\", \"postgap\": " << track.postgap
               << ", \"first_frame\": " << track.firstFrame
               << ", \"file\": \"track" << (track.number < 10 ? "0" : "") << track.number << ".bin\"}";
        output << (i + 1 == tracks.size() ? "\n" : ",\n");
    }
    output << "  ]\n}\n";
}

static void extract(const fs::path &chdPath, const fs::path &outputDir) {
    fs::create_directories(outputDir);
    chd_file *file = nullptr;
    const chd_error openError = chd_open(chdPath.string().c_str(), CHD_OPEN_READ, nullptr, &file);
    if (openError != CHDERR_NONE) fail("Cannot open CHD: " + std::string(chd_error_string(openError)));
    try {
        const chd_header *header = chd_get_header(file);
        if (header->version != CHD_HEADER_VERSION || header->unitbytes < 2352) {
            fail("Unexpected CHD geometry: version " + std::to_string(header->version) +
                 ", unit bytes " + std::to_string(header->unitbytes));
        }
        const auto tracks = readTracks(file);
        writeMetadata(outputDir / "metadata.json", header, tracks);

        std::vector<uint8_t> hunk(header->hunkbytes);
        uint32_t loadedHunk = UINT32_MAX;
        for (const auto &track : tracks) {
            const std::string trackName = std::string("track") + (track.number < 10 ? "0" : "") +
                                          std::to_string(track.number) + ".bin";
            std::ofstream output(outputDir / trackName, std::ios::binary);
            if (!output) fail("Cannot write track output");
            for (int frame = 0; frame < track.frames; ++frame) {
                const uint64_t logicalFrame = track.firstFrame + static_cast<uint64_t>(frame);
                const uint64_t byteOffset = logicalFrame * header->unitbytes;
                const uint32_t hunkNumber = static_cast<uint32_t>(byteOffset / header->hunkbytes);
                const uint32_t hunkOffset = static_cast<uint32_t>(byteOffset % header->hunkbytes);
                if (hunkNumber != loadedHunk) {
                    const chd_error readError = chd_read(file, hunkNumber, hunk.data());
                    if (readError != CHDERR_NONE) fail("Cannot read CHD hunk: " + std::string(chd_error_string(readError)));
                    loadedHunk = hunkNumber;
                }
                output.write(reinterpret_cast<const char *>(hunk.data() + hunkOffset), 2352);
            }
        }
    } catch (...) {
        chd_close(file);
        throw;
    }
    chd_close(file);
}

static void verifyRaw(const fs::path &path) {
    std::ifstream input(path, std::ios::binary | std::ios::ate);
    if (!input) fail("Cannot open raw track: " + path.string());
    const auto size = input.tellg();
    if (size < 0 || size % 2352 != 0) fail("Raw track is not a multiple of 2352 bytes");
    input.seekg(0);
    std::vector<uint8_t> sector(2352);
    uint64_t checked = 0;
    for (int64_t index = 0; index < size / 2352; ++index) {
        input.read(reinterpret_cast<char *>(sector.data()), sector.size());
        if (!input) fail("Short raw track read");
        const bool mode1 = sector[15] == 0x01;
        if (mode1) {
            if (!ecc_verify(sector.data())) fail("ECC verification failed at raw sector " + std::to_string(index));
            ++checked;
        }
    }
    std::cout << "Verified " << checked << " MODE1/2352 sectors in " << path << "\n";
}

static void rewriteEcc(const fs::path &path) {
    std::fstream file(path, std::ios::binary | std::ios::in | std::ios::out);
    if (!file) fail("Cannot open raw track for ECC rewrite: " + path.string());
    file.seekg(0, std::ios::end);
    const auto size = file.tellg();
    if (size < 0 || size % 2352 != 0) fail("Raw track is not a multiple of 2352 bytes");
    file.seekg(0);
    std::vector<uint8_t> sector(2352);
    uint64_t rewritten = 0;
    for (int64_t index = 0; index < size / 2352; ++index) {
        file.seekg(static_cast<std::streamoff>(index * 2352), std::ios::beg);
        file.read(reinterpret_cast<char *>(sector.data()), sector.size());
        if (!file) fail("Short raw track read");
        if (sector[15] == 0x01) {
            ecc_generate(sector.data());
            file.seekp(static_cast<std::streamoff>(index * 2352), std::ios::beg);
            file.write(reinterpret_cast<const char *>(sector.data()), sector.size());
            if (!file) fail("Raw track ECC write failed");
            ++rewritten;
        }
    }
    std::cout << "Rewrote ECC for " << rewritten << " MODE1/2352 sectors in " << path << "\n";
}

static void printUsage() {
    std::cerr << "Usage:\n"
              << "  chd-disc-tool extract <input.chd> <output-dir>\n"
              << "  chd-disc-tool verify-raw <track.bin>\n"
              << "  chd-disc-tool rewrite-ecc <track.bin>\n";
}

int main(int argc, char **argv) {
    try {
        if (argc == 4 && std::string_view(argv[1]) == "extract") {
            extract(argv[2], argv[3]);
            return 0;
        }
        if (argc == 3 && std::string_view(argv[1]) == "verify-raw") {
            verifyRaw(argv[2]);
            return 0;
        }
        if (argc == 3 && std::string_view(argv[1]) == "rewrite-ecc") {
            rewriteEcc(argv[2]);
            return 0;
        }
        printUsage();
        return 2;
    } catch (const std::exception &error) {
        std::cerr << "chd-disc-tool: " << error.what() << "\n";
        return 1;
    }
}

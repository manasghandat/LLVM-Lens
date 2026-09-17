// Bump allocation, and the one place the document's memory is released.
#include "json.h"

#include <cstdlib>
#include <cstring>

namespace jsonq {

namespace {

constexpr std::size_t kChunkBytes = 64 * 1024;
constexpr std::size_t kAlign = alignof(std::max_align_t);

} // namespace

// Payload trails the header, so a chunk is one allocation and one free.
struct Arena::Chunk {
    Chunk *next;
    std::size_t used;
    std::size_t capacity;

    char *data() { return reinterpret_cast<char *>(this + 1); }
};

Arena::~Arena() {
    Chunk *chunk = chunks_;
    while (chunk != nullptr) {
        Chunk *next = chunk->next;
        std::free(chunk);
        chunk = next;
    }
}

void *Arena::alloc(std::size_t bytes) {
    bytes = (bytes + kAlign - 1) & ~(kAlign - 1);
    if (bytes == 0)
        bytes = kAlign;

    Chunk *chunk = chunks_;
    if (chunk == nullptr || chunk->capacity - chunk->used < bytes) {
        std::size_t capacity = kChunkBytes > bytes ? kChunkBytes : bytes;
        auto *fresh = static_cast<Chunk *>(std::malloc(sizeof(Chunk) + capacity));
        if (fresh == nullptr)
            std::abort();
        fresh->next = chunks_;
        fresh->used = 0;
        fresh->capacity = capacity;
        chunks_ = fresh;
        chunk = fresh;
    }

    char *result = chunk->data() + chunk->used;
    chunk->used += bytes;
    bytes_ += bytes;
    return result;
}

std::string_view Arena::copy(std::string_view text) {
    char *buffer = static_cast<char *>(alloc(text.size() + 1));
    std::memcpy(buffer, text.data(), text.size());
    buffer[text.size()] = '\0';
    return std::string_view(buffer, text.size());
}

} // namespace jsonq

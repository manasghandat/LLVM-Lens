// The contract every translation unit of jsonq shares.
//
// Nothing here reaches for a standard container: the arena is the only
// allocator, so a document and everything read out of it are one region.
#ifndef JSONQ_JSON_H
#define JSONQ_JSON_H

#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <string_view>

namespace jsonq {

// --- storage ---------------------------------------------------------------

// A bump allocator: one region, released in one call.
class Arena {
public:
    Arena() = default;
    Arena(const Arena &) = delete;
    Arena &operator=(const Arena &) = delete;
    ~Arena();

    // Aligned for any fundamental type; aborts rather than returning null.
    void *alloc(std::size_t bytes);

    // Copies *text* in and returns a view of the copy, which outlives it.
    std::string_view copy(std::string_view text);

    template <typename T>
    T *make(std::size_t count) {
        return static_cast<T *>(alloc(count * sizeof(T)));
    }

    std::size_t bytes() const { return bytes_; }

private:
    struct Chunk;
    Chunk *chunks_ = nullptr;
    std::size_t bytes_ = 0;
};

// A growable run of T, also built from the arena. Growing copies into a larger
// run and abandons the old one, which is why it is only for small items.
template <typename T>
class Buffer {
public:
    explicit Buffer(Arena &arena) : arena_(&arena) {}

    void push(const T &item) {
        if (count_ == capacity_)
            grow();
        items_[count_++] = item;
    }

    T *data() { return items_; }
    const T *data() const { return items_; }
    std::size_t size() const { return count_; }
    bool empty() const { return count_ == 0; }
    void clear() { count_ = 0; }

private:
    void grow() {
        std::size_t grown = capacity_ == 0 ? 8 : capacity_ * 2;
        T *moved = arena_->make<T>(grown);
        for (std::size_t i = 0; i < count_; i++)
            moved[i] = items_[i];
        items_ = moved;
        capacity_ = grown;
    }

    Arena *arena_;
    T *items_ = nullptr;
    std::size_t count_ = 0;
    std::size_t capacity_ = 0;
};

// --- document --------------------------------------------------------------

enum class Kind : std::uint8_t { Null, Bool, Number, String, Array, Object };

struct Value;

// One key/value pair. Members are sorted by key, so lookup is a binary search.
struct Member {
    std::string_view key;
    const Value *value;
};

struct Value {
    Kind kind = Kind::Null;
    bool boolean = false;
    double number = 0.0;
    std::string_view text;     // String
    const Value *const *items; // Array
    const Member *members;     // Object
    std::size_t count = 0;     // items or members
};

const char *kind_name(Kind kind);

// The member named *key*, or null if there is none or *object* is not one.
const Value *member(const Value &object, std::string_view key);

// --- lexer -----------------------------------------------------------------

enum class Token : std::uint8_t {
    End, Null, True, False, Number, String,
    LBrace, RBrace, LBracket, RBracket, Colon, Comma, Bad,
};

struct Position {
    std::size_t line = 1;
    std::size_t column = 1;
};

class Lexer {
public:
    explicit Lexer(std::string_view text) : text_(text) {}

    Token next();

    double number() const { return number_; }
    std::string_view raw() const { return text_.substr(begin_, offset_ - begin_); }
    Position position() const { return {line_, column_}; }

private:
    void advance();
    Token lex_string();
    Token lex_number();
    Token lex_word();

    std::string_view text_;
    std::size_t offset_ = 0;
    std::size_t begin_ = 0;
    std::size_t line_ = 1;
    std::size_t line_start_ = 0;
    std::size_t column_ = 1;
    double number_ = 0.0;
};

// Decodes the escapes of *quoted*, a String token including both quotes.
bool unescape(Arena &arena, std::string_view quoted, std::string_view &out);

// --- parser ----------------------------------------------------------------

class Parser {
public:
    Parser(Arena &arena, std::string_view text);

    // Parses one document; null on failure, with the reason in error().
    const Value *parse();
    const char *error() const { return error_; }

private:
    void advance();
    const Value *parse_value(int depth);
    const Value *parse_array(int depth);
    const Value *parse_object(int depth);
    Value *node(Kind kind);
    void fail(const char *what);

    Arena &arena_;
    Lexer lexer_;
    Token token_;
    Position at_;
    char error_[160] = {0};
};

// --- query -----------------------------------------------------------------

struct Step {
    enum class Kind : std::uint8_t { Key, Index };
    Kind kind = Kind::Key;
    std::string_view key;
    std::size_t index = 0;
};

// "servers[1].host" as a list of steps, allocated from the arena.
struct Path {
    explicit Path(Arena &arena) : steps(arena) {}
    Buffer<Step> steps;
};

// Parses *text* into *out*; false with a static reason in *error* if malformed.
bool parse_path(std::string_view text, Path &out, const char *&error);

// Replaces *out* with every value *path* selects from the document *root*.
void select(Arena &arena, const Value *root, const Path &path,
            Buffer<const Value *> &out);

// --- output ----------------------------------------------------------------

// Writes *value* as JSON, indenting nested lines one level past *level*.
void print(std::FILE *out, const Value &value, int level = 0);

} // namespace jsonq

#endif // JSONQ_JSON_H

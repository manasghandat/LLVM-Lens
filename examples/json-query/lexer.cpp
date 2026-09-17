// The tokenizer: bytes in, tokens out, with a position to blame on failure.
#include "json.h"

#include <cstdlib>
#include <cstring>

namespace jsonq {

namespace {

bool is_digit(char c) { return c >= '0' && c <= '9'; }

bool is_space(char c) {
    return c == ' ' || c == '\t' || c == '\n' || c == '\r';
}

// JSON has exactly three bare words, and nothing else starts with a letter.
Token keyword(std::string_view word) {
    if (word == "null")
        return Token::Null;
    if (word == "true")
        return Token::True;
    if (word == "false")
        return Token::False;
    return Token::Bad;
}

int hex_digit(char c) {
    if (c >= '0' && c <= '9')
        return c - '0';
    if (c >= 'a' && c <= 'f')
        return c - 'a' + 10;
    if (c >= 'A' && c <= 'F')
        return c - 'A' + 10;
    return -1;
}

// Writes the UTF-8 encoding of *code*, which is what a \u escape denotes.
std::size_t write_utf8(char *out, unsigned code) {
    if (code < 0x80) {
        out[0] = static_cast<char>(code);
        return 1;
    }
    if (code < 0x800) {
        out[0] = static_cast<char>(0xc0 | (code >> 6));
        out[1] = static_cast<char>(0x80 | (code & 0x3f));
        return 2;
    }
    out[0] = static_cast<char>(0xe0 | (code >> 12));
    out[1] = static_cast<char>(0x80 | ((code >> 6) & 0x3f));
    out[2] = static_cast<char>(0x80 | (code & 0x3f));
    return 3;
}

} // namespace

void Lexer::advance() {
    if (text_[offset_] == '\n') {
        line_++;
        line_start_ = offset_ + 1;
    }
    offset_++;
}

Token Lexer::next() {
    while (offset_ < text_.size() && is_space(text_[offset_]))
        advance();

    begin_ = offset_;
    column_ = offset_ - line_start_ + 1;
    if (offset_ >= text_.size())
        return Token::End;

    // None of these can be a newline -- whitespace is already skipped -- so
    // the offset can be stepped without going through advance().
    char c = text_[offset_];
    switch (c) {
    case '{': offset_++; return Token::LBrace;
    case '}': offset_++; return Token::RBrace;
    case '[': offset_++; return Token::LBracket;
    case ']': offset_++; return Token::RBracket;
    case ':': offset_++; return Token::Colon;
    case ',': offset_++; return Token::Comma;
    default: break;
    }

    if (c == '"')
        return lex_string();
    if (c == '-' || is_digit(c))
        return lex_number();
    if (c >= 'a' && c <= 'z')
        return lex_word();

    offset_++; // the offending byte, which nothing consumes
    return Token::Bad;
}

// Scans to the closing quote; the escapes are left for unescape() to decode.
Token Lexer::lex_string() {
    offset_++; // past the opening quote; begin_ already marks it
    while (offset_ < text_.size()) {
        unsigned char c = static_cast<unsigned char>(text_[offset_]);
        if (c == '"') {
            offset_++;
            return Token::String;
        }
        if (c == '\\') {
            offset_ += 2;
            continue;
        }
        // A raw control character is not legal inside a JSON string.
        if (c < 0x20)
            return Token::Bad;
        offset_++;
    }
    return Token::Bad;
}

Token Lexer::lex_number() {
    while (offset_ < text_.size()) {
        char c = text_[offset_];
        if (is_digit(c) || c == '-' || c == '+' || c == '.' || c == 'e' || c == 'E')
            offset_++;
        else
            break;
    }

    // strtod wants a terminator, and the input need not be one.
    char buffer[64];
    std::size_t length = offset_ - begin_;
    if (length == 0 || length >= sizeof(buffer))
        return Token::Bad;
    std::memcpy(buffer, text_.data() + begin_, length);
    buffer[length] = '\0';

    char *end = nullptr;
    number_ = std::strtod(buffer, &end);
    if (end != buffer + length)
        return Token::Bad;
    return Token::Number;
}

Token Lexer::lex_word() {
    while (offset_ < text_.size() && text_[offset_] >= 'a' && text_[offset_] <= 'z')
        offset_++;
    return keyword(text_.substr(begin_, offset_ - begin_));
}

bool unescape(Arena &arena, std::string_view quoted, std::string_view &out) {
    if (quoted.size() < 2 || quoted.front() != '"' || quoted.back() != '"')
        return false;

    // Most strings have no escape at all, and those can be handed out as-is.
    std::string_view body = quoted.substr(1, quoted.size() - 2);
    if (body.find('\\') == std::string_view::npos) {
        out = arena.copy(body);
        return true;
    }

    // Decoding never lengthens, so the escaped size is already an upper bound.
    char *buffer = static_cast<char *>(arena.alloc(body.size()));
    std::size_t written = 0;

    for (std::size_t i = 0; i < body.size(); i++) {
        char c = body[i];
        if (c != '\\') {
            buffer[written++] = c;
            continue;
        }
        if (++i == body.size())
            return false;
        switch (body[i]) {
        case '"':  buffer[written++] = '"';  break;
        case '\\': buffer[written++] = '\\'; break;
        case '/':  buffer[written++] = '/';  break;
        case 'b':  buffer[written++] = '\b'; break;
        case 'f':  buffer[written++] = '\f'; break;
        case 'n':  buffer[written++] = '\n'; break;
        case 'r':  buffer[written++] = '\r'; break;
        case 't':  buffer[written++] = '\t'; break;
        case 'u': {
            if (i + 4 >= body.size())
                return false;
            unsigned code = 0;
            for (int k = 1; k <= 4; k++) {
                int digit = hex_digit(body[i + k]);
                if (digit < 0)
                    return false;
                code = code * 16 + static_cast<unsigned>(digit);
            }
            written += write_utf8(buffer + written, code);
            i += 4;
            break;
        }
        default:
            return false;
        }
    }

    out = std::string_view(buffer, written);
    return true;
}

} // namespace jsonq

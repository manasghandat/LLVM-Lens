// Recursive descent over the token stream, building the document in the arena.
#include "json.h"

namespace jsonq {

namespace {

// Deep enough for real documents, shallow enough that a hostile one runs out
// of depth before it runs out of stack.
constexpr int kMaxDepth = 64;

// Members arrive in document order; lookups need them sorted. Objects are
// small, so insertion sort is the right size of tool.
void sort_members(Member *members, std::size_t count) {
    for (std::size_t i = 1; i < count; i++) {
        Member held = members[i];
        std::size_t j = i;
        while (j > 0 && held.key < members[j - 1].key) {
            members[j] = members[j - 1];
            j--;
        }
        members[j] = held;
    }
}

} // namespace

Parser::Parser(Arena &arena, std::string_view text)
    : arena_(arena), lexer_(text), token_(lexer_.next()), at_(lexer_.position()) {}

void Parser::advance() {
    token_ = lexer_.next();
    at_ = lexer_.position();
}

// Only the first failure is reported; the rest are consequences of it.
void Parser::fail(const char *what) {
    if (error_[0] != '\0')
        return;
    std::snprintf(error_, sizeof(error_), "line %zu, column %zu: %s",
                  at_.line, at_.column, what);
}

Value *Parser::node(Kind kind) {
    Value *value = arena_.make<Value>(1);
    *value = Value();
    value->kind = kind;
    return value;
}

const Value *Parser::parse() {
    const Value *root = parse_value(0);
    if (root == nullptr)
        return nullptr;
    if (token_ != Token::End) {
        fail("trailing text after the document");
        return nullptr;
    }
    return root;
}

const Value *Parser::parse_value(int depth) {
    if (depth > kMaxDepth) {
        fail("nested too deeply");
        return nullptr;
    }

    switch (token_) {
    case Token::Null:
        advance();
        return node(Kind::Null);

    case Token::True:
    case Token::False: {
        bool literal = token_ == Token::True;
        advance();
        Value *value = node(Kind::Bool);
        value->boolean = literal;
        return value;
    }

    case Token::Number: {
        double number = lexer_.number();
        advance();
        Value *value = node(Kind::Number);
        value->number = number;
        return value;
    }

    case Token::String: {
        std::string_view decoded;
        if (!unescape(arena_, lexer_.raw(), decoded)) {
            fail("invalid escape sequence in string");
            return nullptr;
        }
        advance();
        Value *value = node(Kind::String);
        value->text = decoded;
        return value;
    }

    case Token::LBracket:
        return parse_array(depth);

    case Token::LBrace:
        return parse_object(depth);

    default:
        fail("expected a value");
        return nullptr;
    }
}

const Value *Parser::parse_array(int depth) {
    advance(); // '['

    Buffer<const Value *> items(arena_);
    if (token_ == Token::RBracket) {
        advance();
    } else {
        for (;;) {
            const Value *item = parse_value(depth + 1);
            if (item == nullptr)
                return nullptr;
            items.push(item);

            if (token_ == Token::Comma) {
                advance();
                continue;
            }
            if (token_ != Token::RBracket) {
                fail("expected ',' or ']' in array");
                return nullptr;
            }
            advance();
            break;
        }
    }

    // The buffer is arena memory, so it can be handed over instead of copied.
    Value *value = node(Kind::Array);
    value->count = items.size();
    value->items = items.data();
    return value;
}

const Value *Parser::parse_object(int depth) {
    advance(); // '{'

    Buffer<Member> members(arena_);
    if (token_ == Token::RBrace) {
        advance();
    } else {
        for (;;) {
            if (token_ != Token::String) {
                fail("expected a member name");
                return nullptr;
            }
            std::string_view key;
            if (!unescape(arena_, lexer_.raw(), key)) {
                fail("invalid escape sequence in member name");
                return nullptr;
            }
            advance();

            if (token_ != Token::Colon) {
                fail("expected ':' after a member name");
                return nullptr;
            }
            advance();

            const Value *held = parse_value(depth + 1);
            if (held == nullptr)
                return nullptr;

            Member entry;
            entry.key = key;
            entry.value = held;
            members.push(entry);

            if (token_ == Token::Comma) {
                advance();
                continue;
            }
            if (token_ != Token::RBrace) {
                fail("expected ',' or '}' in object");
                return nullptr;
            }
            advance();
            break;
        }
    }

    sort_members(members.data(), members.size());

    Value *value = node(Kind::Object);
    value->count = members.size();
    value->members = members.data();
    return value;
}

} // namespace jsonq

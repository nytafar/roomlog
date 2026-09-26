package no.nyta.roomlog.core

import java.math.BigDecimal
import java.math.MathContext
import java.math.RoundingMode

/**
 * Minimal JSON for sidecars: objects are [LinkedHashMap] (insertion order is
 * wire order), integers are [Long], other numbers [Double].
 *
 * [dumps] reproduces Python's `json.dumps(obj, ensure_ascii=True, ...)`
 * byte for byte for the values a sidecar holds: `separators=(",", ":")` when
 * `indent` is null, `indent=2` with `(",", ": ")` otherwise, and floats in
 * Python's `repr` form (shortest round-trip, `1.0` not `1`).
 */
object Json {
    class ParseError(message: String) : IllegalArgumentException(message)

    // -- writing -----------------------------------------------------------

    fun dumps(value: Any?, indent: Int? = null): String =
        StringBuilder().also { write(it, value, indent, 0) }.toString()

    private fun write(sb: StringBuilder, v: Any?, indent: Int?, level: Int) {
        when (v) {
            null -> sb.append("null")
            is Boolean -> sb.append(if (v) "true" else "false")
            is Int, is Long, is Short, is Byte -> sb.append(v.toString())
            is Double -> sb.append(pyRepr(v))
            is Float -> sb.append(pyRepr(v.toDouble()))
            is String -> writeString(sb, v)
            is Map<*, *> -> writeContainer(sb, '{', '}', v.entries.toList(), indent, level) { e ->
                e as Map.Entry<*, *>
                writeString(sb, e.key as? String ?: throw IllegalArgumentException("non-string key ${e.key}"))
                sb.append(if (indent == null) ":" else ": ")
                write(sb, e.value, indent, level + 1)
            }
            is List<*> -> writeContainer(sb, '[', ']', v, indent, level) { write(sb, it, indent, level + 1) }
            else -> throw IllegalArgumentException("cannot serialise ${v::class}")
        }
    }

    private fun <T> writeContainer(
        sb: StringBuilder, open: Char, close: Char, items: List<T>, indent: Int?, level: Int, item: (T) -> Unit,
    ) {
        sb.append(open)
        if (items.isEmpty()) {
            sb.append(close)
            return
        }
        items.forEachIndexed { i, x ->
            if (i > 0) sb.append(',')
            if (indent != null) sb.append('\n').append(" ".repeat(indent * (level + 1)))
            item(x)
        }
        if (indent != null) sb.append('\n').append(" ".repeat(indent * level))
        sb.append(close)
    }

    private fun writeString(sb: StringBuilder, s: String) {
        sb.append('"')
        for (c in s) {
            when (c) {
                '"' -> sb.append("\\\"")
                '\\' -> sb.append("\\\\")
                '\n' -> sb.append("\\n")
                '\r' -> sb.append("\\r")
                '\t' -> sb.append("\\t")
                '\b' -> sb.append("\\b")
                '\u000c' -> sb.append("\\f")
                else -> if (c in ' '..'~') sb.append(c) else sb.append("\\u").append(c.code.toString(16).padStart(4, '0'))
            }
        }
        sb.append('"')
    }

    /** Python's `repr(float)`: shortest string that round-trips, plain
     *  notation for 1e-4 <= abs(x) < 1e16, `1e-05` style outside it. */
    fun pyRepr(d: Double): String {
        require(!d.isNaN() && !d.isInfinite()) { "NaN/Infinity are not JSON" }
        if (d == 0.0) return if (1.0 / d < 0) "-0.0" else "0.0"
        val exact = BigDecimal(d)
        var shortest: BigDecimal = exact
        for (p in 1..17) {
            val r = exact.round(MathContext(p, RoundingMode.HALF_EVEN))
            if (r.toDouble() == d) {
                shortest = r
                break
            }
        }
        val digits = shortest.unscaledValue().abs().toString().trimEnd('0').ifEmpty { "0" }
        // decimal exponent of the leading digit
        val exp = shortest.precision() - shortest.scale() - 1
        val sign = if (d < 0) "-" else ""
        return if (exp >= -4 && exp < 16) {
            val plain = shortest.abs().stripTrailingZeros().toPlainString()
            sign + if ('.' in plain) plain else "$plain.0"
        } else {
            val mant = if (digits.length == 1) digits else digits[0] + "." + digits.substring(1)
            val e = if (exp < 0) "-" + (-exp).toString().padStart(2, '0') else "+" + exp.toString().padStart(2, '0')
            "$sign${mant}e$e"
        }
    }

    // -- parsing -----------------------------------------------------------

    fun parse(text: String): Any? {
        val p = Parser(text)
        p.ws()
        val v = p.value()
        p.ws()
        if (p.i != text.length) throw ParseError("trailing data at ${p.i}")
        return v
    }

    @Suppress("UNCHECKED_CAST")
    fun parseObject(text: String): LinkedHashMap<String, Any?> =
        parse(text) as? LinkedHashMap<String, Any?> ?: throw ParseError("not a JSON object")

    private class Parser(val s: String) {
        var i = 0

        fun ws() {
            while (i < s.length && s[i] in " \t\r\n") i++
        }

        fun fail(msg: String): Nothing = throw ParseError("$msg at $i")

        fun value(): Any? {
            if (i >= s.length) fail("unexpected end")
            return when (val c = s[i]) {
                '{' -> obj()
                '[' -> arr()
                '"' -> str()
                't' -> lit("true", true)
                'f' -> lit("false", false)
                'n' -> lit("null", null)
                else -> if (c == '-' || c in '0'..'9') num() else fail("unexpected '$c'")
            }
        }

        fun lit(word: String, v: Any?): Any? {
            if (!s.startsWith(word, i)) fail("bad literal")
            i += word.length
            return v
        }

        fun obj(): LinkedHashMap<String, Any?> {
            val m = LinkedHashMap<String, Any?>()
            i++
            ws()
            if (i < s.length && s[i] == '}') {
                i++
                return m
            }
            while (true) {
                ws()
                if (i >= s.length || s[i] != '"') fail("expected key")
                val k = str()
                ws()
                if (i >= s.length || s[i] != ':') fail("expected ':'")
                i++
                ws()
                m[k] = value()
                ws()
                if (i >= s.length) fail("unterminated object")
                when (s[i++]) {
                    ',' -> continue
                    '}' -> return m
                    else -> fail("expected ',' or '}'")
                }
            }
        }

        fun arr(): List<Any?> {
            val l = ArrayList<Any?>()
            i++
            ws()
            if (i < s.length && s[i] == ']') {
                i++
                return l
            }
            while (true) {
                ws()
                l.add(value())
                ws()
                if (i >= s.length) fail("unterminated array")
                when (s[i++]) {
                    ',' -> continue
                    ']' -> return l
                    else -> fail("expected ',' or ']'")
                }
            }
        }

        fun str(): String {
            val sb = StringBuilder()
            i++
            while (true) {
                if (i >= s.length) fail("unterminated string")
                val c = s[i++]
                when {
                    c == '"' -> return sb.toString()
                    c == '\\' -> {
                        if (i >= s.length) fail("bad escape")
                        when (val e = s[i++]) {
                            '"' -> sb.append('"')
                            '\\' -> sb.append('\\')
                            '/' -> sb.append('/')
                            'b' -> sb.append('\b')
                            'f' -> sb.append('\u000c')
                            'n' -> sb.append('\n')
                            'r' -> sb.append('\r')
                            't' -> sb.append('\t')
                            'u' -> {
                                if (i + 4 > s.length) fail("bad \\u escape")
                                sb.append(s.substring(i, i + 4).toIntOrNull(16)?.toChar() ?: fail("bad \\u escape"))
                                i += 4
                            }
                            else -> fail("bad escape '\\$e'")
                        }
                    }
                    c < ' ' -> fail("control character in string")
                    else -> sb.append(c)
                }
            }
        }

        fun num(): Any {
            val start = i
            if (s[i] == '-') i++
            while (i < s.length && s[i] in '0'..'9') i++
            var isFloat = false
            if (i < s.length && s[i] == '.') {
                isFloat = true
                i++
                while (i < s.length && s[i] in '0'..'9') i++
            }
            if (i < s.length && (s[i] == 'e' || s[i] == 'E')) {
                isFloat = true
                i++
                if (i < s.length && (s[i] == '+' || s[i] == '-')) i++
                while (i < s.length && s[i] in '0'..'9') i++
            }
            val t = s.substring(start, i)
            return (if (isFloat) t.toDoubleOrNull() else t.toLongOrNull()) ?: fail("bad number '$t'")
        }
    }
}

"""Extract only a provisional top-level answer string from actual JSON deltas."""
import json


class AnswerDraft:
    def __init__(self, emit):
        self.emit = emit
        self.raw = ""
        self.displayed = ""

    def feed(self, delta: str):
        self.raw += delta
        if len(self.raw) > 65536:
            raise ValueError("model stream exceeds output budget")
        decoder = json.JSONDecoder()
        text, index = self.raw, 0
        try:
            index = self._spaces(text, index)
            if text[index] != "{":
                return
            index += 1
            while True:
                index = self._spaces(text, index)
                key, index = decoder.raw_decode(text, index)
                if not isinstance(key, str):
                    return
                index = self._spaces(text, index)
                if text[index] != ":":
                    return
                index = self._spaces(text, index + 1)
                if key == "answer":
                    if text[index] != '"':
                        return
                    start, index = index + 1, index + 1
                    escaped = False
                    while index < len(text):
                        char = text[index]
                        if char == '"' and not escaped:
                            break
                        if char == "\\" and not escaped:
                            escaped = True
                        else:
                            escaped = False
                        index += 1
                    fragment = text[start:index]
                    # Trailing incomplete escape/Unicode sequence waits for the next real delta.
                    while fragment:
                        try:
                            answer = json.loads('"' + fragment + '"')
                            break
                        except ValueError:
                            fragment = fragment[:-1]
                    else:
                        answer = ""
                    # Don't display a lone UTF16 surrogate when provider splits \\u pairs.
                    while answer and 0xD800 <= ord(answer[-1]) <= 0xDFFF:
                        answer = answer[:-1]
                    if any(0xD800 <= ord(c) <= 0xDFFF for c in answer):
                        return
                    if answer.startswith(self.displayed):
                        addition = answer[len(self.displayed):]
                        if addition:
                            self.emit(addition)
                            self.displayed = answer
                    return
                _, index = decoder.raw_decode(text, index)
                index = self._spaces(text, index)
                if text[index] != ",":
                    return
                index += 1
        except (ValueError, IndexError):
            # This is merely a partial draft; strict final JSON/schema/citation validation is unchanged.
            return

    @staticmethod
    def _spaces(text, index):
        while index < len(text) and text[index] in " \r\n\t":
            index += 1
        return index

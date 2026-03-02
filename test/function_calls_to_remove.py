# module-level function def → goes to function_defs
def top_level():
    inner_call()  # NOT collected (inside function body)


# module-level bare call → goes to function_calls
top_level()


class Example:
    class_call()  # bare call in class body → goes to function_calls

    def method(self):
        method_call()  # NOT collected (inside function body)

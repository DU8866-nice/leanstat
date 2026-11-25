import Statements

/-! Module docs. This file has no `sorry`, /- even nested -/ in comments. -/

namespace Lib

-- sorry in a line comment
@[simp] theorem a_thm : Statement := trivial

private noncomputable def msg : String := "sorry -- /- not a comment -/"
def quote : Char := '"'
def after : Nat := 1 -- the char literal above must not open a string

instance (priority := 100) : Inhabited Nat := ⟨0⟩
class inductive Tag | a | b
example : True := trivial

end Lib

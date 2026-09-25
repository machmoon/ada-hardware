import kaleoMark from "@/assets/kaleo-mark.png";
import { StepFrame } from "@/components/setup";

export const HELLO_TITLE = "Hello.";

/** The brand mark is allowed here: this is the dashboard surface, not the strip. */
export const HelloStep = () => (
  <StepFrame
    stepId="hello"
    title={HELLO_TITLE}
    subtitle="Ada is an AI hardware engineer that works beside KiCad: describe a board, get a schematic, a placed and routed board and a printable case, each checked by KiCad's own ERC and DRC. A few choices, then you are set."
    above={<img src={kaleoMark} alt="" width={72} height={72} className="mx-auto rounded-2xl" draggable={false} />}
  />
);

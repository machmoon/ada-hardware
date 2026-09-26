// Band M1: the board, revealed by Aceternity's container-scroll-animation inside the feature page's band header.
import boardTop from "../../assets/board_top.png";
import { ContainerScroll } from "@/components/aceternity/container-scroll-animation";
import { REVEAL } from "@/content";

export function BoardReveal() {
  return (
    <section aria-labelledby="reveal-title" className="w-full">
      <ContainerScroll
        titleComponent={
          <div className="mx-auto flex w-full max-w-[680px] flex-col items-center gap-3 pb-12 text-center">
            <h2
              id="reveal-title"
              className="font-serif text-[28px] leading-[1.2] font-[600] text-[var(--text-primary)] md:text-[40px]"
            >
              {REVEAL.title}
            </h2>
            <p className="text-base leading-6 font-normal text-[var(--text-secondary)]">{REVEAL.body}</p>
          </div>
        }
      >
        <img
          src={boardTop}
          alt={REVEAL.imageAlt}
          width={1674}
          height={834}
          loading="lazy"
          decoding="async"
          className="h-full w-full object-contain"
        />
      </ContainerScroll>
    </section>
  );
}

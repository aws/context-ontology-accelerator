// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Memoized Markdown renderer for streaming AI responses.
 *
 * Splits markdown into blocks via marked.lexer() and memoizes each completed
 * block. Only the last (in-progress) block re-renders on each token flush.
 * Pattern from Vercel AI SDK cookbook.
 */
import { memo, useMemo } from "react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import TextContent from "@cloudscape-design/components/text-content";
import { marked } from "marked";

interface MemoizedMarkdownBlockProps {
  content: string;
}

const MemoizedMarkdownBlock = memo(
  ({ content }: MemoizedMarkdownBlockProps) => {
    return (
      <TextContent>
        <Markdown remarkPlugins={[remarkGfm]}>{content}</Markdown>
      </TextContent>
    );
  },
);

MemoizedMarkdownBlock.displayName = "MemoizedMarkdownBlock";

interface MemoizedMarkdownProps {
  content: string;
  id: string;
}

/**
 * Renders markdown content split into memoized blocks.
 * Completed blocks are cached; only the trailing block re-renders.
 */
export const MemoizedMarkdown = memo(
  ({ content, id }: MemoizedMarkdownProps) => {
    const blocks = useMemo(() => {
      try {
        const tokens = marked.lexer(content);
        return tokens.map((token) => token.raw);
      } catch {
        // Fallback: render as single block if lexer fails on incomplete markdown
        return [content];
      }
    }, [content]);

    return (
      <>
        {blocks.map((block, index) => (
          <MemoizedMarkdownBlock content={block} key={`${id}-block-${index}`} />
        ))}
      </>
    );
  },
);

MemoizedMarkdown.displayName = "MemoizedMarkdown";

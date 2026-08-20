import { LexicalComposer } from "@lexical/react/LexicalComposer";
import { ContentEditable } from "@lexical/react/LexicalContentEditable";
import { LexicalErrorBoundary } from "@lexical/react/LexicalErrorBoundary";
import { OnChangePlugin } from "@lexical/react/LexicalOnChangePlugin";
import { RichTextPlugin } from "@lexical/react/LexicalRichTextPlugin";
import { useLexicalComposerContext } from "@lexical/react/LexicalComposerContext";
import {
  $createParagraphNode,
  $createTextNode,
  $getNodeByKey,
  $getRoot,
  $getSelection,
  $isElementNode,
  $isLineBreakNode,
  $isNodeSelection,
  $isRangeSelection,
  $isTextNode,
  $nodesOfType,
  COMMAND_PRIORITY_HIGH,
  createCommand,
  DecoratorNode,
  KEY_BACKSPACE_COMMAND,
  KEY_DELETE_COMMAND,
  KEY_ENTER_COMMAND,
  type EditorConfig,
  type EditorState,
  type LexicalCommand,
  type LexicalEditor,
  type LexicalNode,
  type NodeKey,
  type SerializedLexicalNode,
  type Spread,
} from "lexical";
import { Sparkles, X } from "lucide-react";
import {
  forwardRef,
  useEffect,
  useImperativeHandle,
  useRef,
  type MutableRefObject,
  type ReactNode,
} from "react";
import type { Skill, WorkflowMessagePart } from "./types";

type SkillReference = Pick<Skill, "id" | "name">;

type SerializedSkillTokenNode = Spread<
  {
    skillId: string;
    skillName: string;
  },
  SerializedLexicalNode
>;

const INSERT_SKILL_COMMAND: LexicalCommand<SkillReference> = createCommand("INSERT_SKILL_COMMAND");
const REMOVE_SKILL_COMMAND: LexicalCommand<string> = createCommand("REMOVE_SKILL_COMMAND");
const CLEAR_PROMPT_COMMAND: LexicalCommand<void> = createCommand("CLEAR_PROMPT_COMMAND");
const SET_PROMPT_COMMAND: LexicalCommand<WorkflowMessagePart[]> = createCommand("SET_PROMPT_COMMAND");

class SkillTokenNode extends DecoratorNode<ReactNode> {
  __skillId: string;
  __skillName: string;

  static getType(): string {
    return "skill-token";
  }

  static clone(node: SkillTokenNode): SkillTokenNode {
    return new SkillTokenNode(node.__skillId, node.__skillName, node.__key);
  }

  static importJSON(serialized: SerializedSkillTokenNode): SkillTokenNode {
    return new SkillTokenNode(serialized.skillId, serialized.skillName);
  }

  constructor(skillId: string, skillName: string, key?: NodeKey) {
    super(key);
    this.__skillId = skillId;
    this.__skillName = skillName;
  }

  exportJSON(): SerializedSkillTokenNode {
    return {
      ...super.exportJSON(),
      type: "skill-token",
      version: 1,
      skillId: this.__skillId,
      skillName: this.__skillName,
    };
  }

  createDOM(_config: EditorConfig): HTMLElement {
    const element = document.createElement("span");
    element.className = "agent-start-skill-node";
    return element;
  }

  updateDOM(): false {
    return false;
  }

  isInline(): true {
    return true;
  }

  isIsolated(): true {
    return true;
  }

  isKeyboardSelectable(): true {
    return true;
  }

  getTextContent(): string {
    return `【Skill：${this.__skillName}】`;
  }

  decorate(): ReactNode {
    return (
      <SkillToken
        nodeKey={this.getKey()}
        skillId={this.__skillId}
        skillName={this.__skillName}
      />
    );
  }
}

function $createSkillTokenNode(skill: SkillReference): SkillTokenNode {
  return new SkillTokenNode(skill.id, skill.name);
}

function $isSkillTokenNode(node: LexicalNode | null | undefined): node is SkillTokenNode {
  return node instanceof SkillTokenNode;
}

function SkillToken({ nodeKey, skillId, skillName }: { nodeKey: NodeKey; skillId: string; skillName: string }) {
  const [editor] = useLexicalComposerContext();
  return (
    <span className="agent-start-skill-token" data-skill-id={skillId} title={skillName}>
      <span className="agent-start-skill-token-icon"><Sparkles /></span>
      <strong>{skillName}</strong>
      <button
        type="button"
        title="移除 Skill"
        aria-label={`移除 Skill：${skillName}`}
        onMouseDown={(event) => event.preventDefault()}
        onClick={(event) => {
          event.preventDefault();
          event.stopPropagation();
          editor.update(() => {
            const node = $getNodeByKey(nodeKey);
            if ($isSkillTokenNode(node)) node.remove();
          });
          editor.focus();
        }}
      >
        <X />
      </button>
    </span>
  );
}

function appendText(parts: WorkflowMessagePart[], text: string) {
  if (!text) return;
  const previous = parts.at(-1);
  if (previous?.type === "text") previous.text += text;
  else parts.push({ type: "text", text });
}

function visitNode(node: LexicalNode, parts: WorkflowMessagePart[]) {
  if ($isSkillTokenNode(node)) {
    parts.push({
      type: "skill_ref",
      skill_id: node.__skillId,
      skill_name: node.__skillName,
    });
  } else if ($isTextNode(node)) {
    appendText(parts, node.getTextContent());
  } else if ($isLineBreakNode(node)) {
    appendText(parts, "\n");
  } else if ($isElementNode(node)) {
    node.getChildren().forEach((child) => visitNode(child, parts));
  }
}

function readMessageParts(): WorkflowMessagePart[] {
  const parts: WorkflowMessagePart[] = [];
  const children = $getRoot().getChildren();
  children.forEach((child, index) => {
    visitNode(child, parts);
    if (index < children.length - 1) appendText(parts, "\n");
  });
  while (parts[0]?.type === "text" && !parts[0].text) parts.shift();
  while (parts.at(-1)?.type === "text" && !(parts.at(-1) as { type: "text"; text: string }).text) parts.pop();
  return parts;
}

function $removeAdjacentSkill(direction: "backward" | "forward"): boolean {
  const selection = $getSelection();
  if ($isNodeSelection(selection)) {
    const skills = selection.getNodes().filter($isSkillTokenNode);
    if (!skills.length) return false;
    skills.forEach((node) => node.remove());
    return true;
  }
  if (!$isRangeSelection(selection) || !selection.isCollapsed()) return false;

  const point = selection.anchor;
  const node = point.getNode();
  let candidate: LexicalNode | null = null;

  if ($isTextNode(node)) {
    const text = node.getTextContent();
    if (direction === "backward") {
      if (point.offset === 0) {
        candidate = node.getPreviousSibling();
      } else if (!text.slice(0, point.offset).trim()) {
        candidate = node.getPreviousSibling();
        if ($isSkillTokenNode(candidate)) {
          node.setTextContent(text.slice(point.offset));
          node.selectStart();
        }
      }
    } else if (point.offset === text.length) {
      candidate = node.getNextSibling();
    } else if (!text.slice(point.offset).trim()) {
      candidate = node.getNextSibling();
      if ($isSkillTokenNode(candidate)) {
        node.setTextContent(text.slice(0, point.offset));
        node.selectEnd();
      }
    }
  } else if ($isElementNode(node)) {
    candidate = node.getChildAtIndex(
      direction === "backward" ? point.offset - 1 : point.offset,
    );
  }

  if (!$isSkillTokenNode(candidate)) return false;
  candidate.remove();
  return true;
}

function EditorCommandsPlugin({ onSubmitRequest }: { onSubmitRequest: () => void }) {
  const [editor] = useLexicalComposerContext();
  useEffect(
    () => editor.registerCommand(
      INSERT_SKILL_COMMAND,
      (skill) => {
        if ($nodesOfType(SkillTokenNode).some((node) => node.__skillId === skill.id)) return true;
        const token = $createSkillTokenNode(skill);
        const space = $createTextNode(" ");
        const selection = $getSelection();
        if ($isRangeSelection(selection)) {
          selection.insertNodes([token, space]);
          space.selectEnd();
        } else {
          const root = $getRoot();
          const emptyLine = root.getLastChild();
          if ($isElementNode(emptyLine) && emptyLine.getChildrenSize() === 0) {
            emptyLine.append(token, space);
          } else {
            const paragraph = $createParagraphNode();
            paragraph.append(token, space);
            root.append(paragraph);
          }
          space.selectEnd();
        }
        return true;
      },
      COMMAND_PRIORITY_HIGH,
    ),
    [editor],
  );
  useEffect(
    () => editor.registerCommand(
      SET_PROMPT_COMMAND,
      (parts) => {
        const root = $getRoot();
        root.clear();
        const paragraph = $createParagraphNode();
        parts.forEach((part) => {
          if (part.type === "text") paragraph.append($createTextNode(part.text));
          else paragraph.append($createSkillTokenNode({ id: part.skill_id, name: part.skill_name }));
        });
        root.append(paragraph);
        paragraph.selectEnd();
        return true;
      },
      COMMAND_PRIORITY_HIGH,
    ),
    [editor],
  );
  useEffect(
    () => editor.registerCommand(
      REMOVE_SKILL_COMMAND,
      (skillId) => {
        $nodesOfType(SkillTokenNode)
          .filter((node) => node.__skillId === skillId)
          .forEach((node) => node.remove());
        return true;
      },
      COMMAND_PRIORITY_HIGH,
    ),
    [editor],
  );
  useEffect(
    () => editor.registerCommand(
      CLEAR_PROMPT_COMMAND,
      () => {
        $getRoot().clear();
        return true;
      },
      COMMAND_PRIORITY_HIGH,
    ),
    [editor],
  );
  useEffect(
    () => editor.registerCommand(
      KEY_ENTER_COMMAND,
      (event) => {
        if (!event || event.shiftKey || event.isComposing) return false;
        event.preventDefault();
        onSubmitRequest();
        return true;
      },
      COMMAND_PRIORITY_HIGH,
    ),
    [editor, onSubmitRequest],
  );
  useEffect(
    () => editor.registerCommand(
      KEY_BACKSPACE_COMMAND,
      () => $removeAdjacentSkill("backward"),
      COMMAND_PRIORITY_HIGH,
    ),
    [editor],
  );
  useEffect(
    () => editor.registerCommand(
      KEY_DELETE_COMMAND,
      () => $removeAdjacentSkill("forward"),
      COMMAND_PRIORITY_HIGH,
    ),
    [editor],
  );
  return null;
}

function EditorLifecyclePlugin({
  editorRef,
  disabled,
}: {
  editorRef: MutableRefObject<LexicalEditor | null>;
  disabled: boolean;
}) {
  const [editor] = useLexicalComposerContext();
  useEffect(() => {
    editorRef.current = editor;
    return () => {
      if (editorRef.current === editor) editorRef.current = null;
    };
  }, [editor, editorRef]);
  useEffect(() => editor.setEditable(!disabled), [disabled, editor]);
  return null;
}

export interface SkillPromptEditorHandle {
  insertSkill: (skill: SkillReference) => void;
  removeSkill: (skillId: string) => void;
  clear: () => void;
  setParts: (parts: WorkflowMessagePart[]) => void;
  focus: () => void;
}

interface SkillPromptEditorProps {
  disabled?: boolean;
  onChange: (parts: WorkflowMessagePart[]) => void;
  onSubmitRequest: () => void;
}

export const SkillPromptEditor = forwardRef<SkillPromptEditorHandle, SkillPromptEditorProps>(
  function SkillPromptEditor({ disabled = false, onChange, onSubmitRequest }, ref) {
    const editorRef = useRef<LexicalEditor | null>(null);
    useImperativeHandle(ref, () => ({
      insertSkill(skill) {
        editorRef.current?.dispatchCommand(INSERT_SKILL_COMMAND, skill);
        editorRef.current?.focus();
      },
      removeSkill(skillId) {
        editorRef.current?.dispatchCommand(REMOVE_SKILL_COMMAND, skillId);
        editorRef.current?.focus();
      },
      clear() {
        editorRef.current?.dispatchCommand(CLEAR_PROMPT_COMMAND, undefined);
      },
      setParts(parts) {
        editorRef.current?.dispatchCommand(SET_PROMPT_COMMAND, parts);
        editorRef.current?.focus();
      },
      focus() {
        editorRef.current?.focus();
      },
    }), []);

    const handleChange = (state: EditorState) => {
      state.read(() => onChange(readMessageParts()));
    };

    return (
      <LexicalComposer
        initialConfig={{
          namespace: "SkillGoTaskPrompt",
          nodes: [SkillTokenNode],
          onError(error) {
            throw error;
          },
          theme: {
            paragraph: "agent-start-editor-paragraph",
          },
        }}
      >
        <div className="agent-start-editor-shell">
          <RichTextPlugin
            contentEditable={
              <ContentEditable
                className="agent-start-rich-editor"
                aria-label="描述你的任务"
                aria-placeholder="直接描述任务；需要指定顺序时，可在句中插入 Skill…"
                placeholder={
                  <div className="agent-start-editor-placeholder">
                    直接描述任务；需要指定顺序时，可在句中插入 Skill…
                  </div>
                }
              />
            }
            ErrorBoundary={LexicalErrorBoundary}
          />
        </div>
        <OnChangePlugin onChange={handleChange} ignoreSelectionChange />
        <EditorLifecyclePlugin editorRef={editorRef} disabled={disabled} />
        <EditorCommandsPlugin onSubmitRequest={onSubmitRequest} />
      </LexicalComposer>
    );
  },
);
